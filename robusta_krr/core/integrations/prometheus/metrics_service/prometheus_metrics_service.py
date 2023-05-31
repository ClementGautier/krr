import asyncio
import datetime
from typing import Optional, no_type_check

import requests
from kubernetes.client import ApiClient
from prometheus_api_client import PrometheusConnect, Retry
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, HTTPError

from robusta_krr.core.abstract.strategies import ResourceHistoryData
from robusta_krr.core.models.config import Config
from robusta_krr.core.models.objects import K8sObjectData
from robusta_krr.core.models.result import ResourceType
from robusta_krr.utils.configurable import Configurable
from robusta_krr.utils.service_discovery import ServiceDiscovery

from ..metrics import BaseMetricLoader
from .base_metric_service import MetricsNotFound, MetricsService


class PrometheusDiscovery(ServiceDiscovery):
    def find_metrics_url(self, *, api_client: Optional[ApiClient] = None) -> Optional[str]:
        """
        Finds the Prometheus URL using selectors.
        Args:
            api_client (Optional[ApiClient]): A Kubernetes API client. Defaults to None.
        Returns:
            Optional[str]: The discovered Prometheus URL, or None if not found.
        """

        return super().find_url(
            selectors=[
                "app=kube-prometheus-stack-prometheus",
                "app=prometheus,component=server",
                "app=prometheus-server",
                "app=prometheus-operator-prometheus",
                "app=prometheus-msteams",
                "app=rancher-monitoring-prometheus",
                "app=prometheus-prometheus",
            ]
        )


class PrometheusNotFound(MetricsNotFound):
    """
    An exception raised when Prometheus is not found.
    """

    pass


class CustomPrometheusConnect(PrometheusConnect):
    """
    Custom PrometheusConnect class to handle retries.
    """

    @no_type_check
    def __init__(
        self,
        url: str = "http://127.0.0.1:9090",
        headers: dict = None,
        disable_ssl: bool = False,
        retry: Retry = None,
        auth: tuple = None,
    ):
        super().__init__(url, headers, disable_ssl, retry, auth)
        self._session = requests.Session()
        self._session.mount(self.url, HTTPAdapter(max_retries=retry, pool_maxsize=10, pool_block=True))


class PrometheusMetricsService(MetricsService):
    """
    A class for fetching metrics from Prometheus.
    """

    def __init__(
        self,
        config: Config,
        *,
        cluster: Optional[str] = None,
        api_client: Optional[ApiClient] = None,
        service_discovery: ServiceDiscovery = PrometheusDiscovery,
    ) -> None:
        super().__init__(config=config, api_client=api_client, cluster=cluster)

        self.info(f"Connecting to {self.name()} for {self.cluster} cluster")

        self.auth_header = self.config.prometheus_auth_header
        self.ssl_enabled = self.config.prometheus_ssl_enabled

        self.prometheus_discovery = service_discovery(config=self.config, api_client=self.api_client)

        self.url = self.config.prometheus_url
        self.url = self.url or self.prometheus_discovery.find_metrics_url()

        if not self.url:
            raise PrometheusNotFound(
                f"{self.name()} instance could not be found while scanning in {self.cluster} cluster.\n"
                "\tTry using port-forwarding and/or setting the url manually (using the -p flag.)."
            )

        self.info(f"Using {self.name()} at {self.url} for cluster {cluster or 'default'}")

        headers = {}

        if self.auth_header:
            headers = {"Authorization": self.auth_header}
        elif not self.config.inside_cluster:
            self.api_client.update_params_for_auth(headers, {}, ["BearerToken"])

        self.prometheus = CustomPrometheusConnect(url=self.url, disable_ssl=not self.ssl_enabled, headers=headers)

    def check_connection(self):
        """
        Checks the connection to Prometheus.
        Raises:
            PrometheusNotFound: If the connection to Prometheus cannot be established.
        """
        try:
            response = self.prometheus._session.get(
                f"{self.prometheus.url}/api/v1/query",
                verify=self.prometheus.ssl_verification,
                headers=self.prometheus.headers,
                # This query should return empty results, but is correct
                params={"query": "example"},
            )
            response.raise_for_status()
        except (ConnectionError, HTTPError) as e:
            raise PrometheusNotFound(
                f"Couldn't connect to Prometheus found under {self.prometheus.url}\nCaused by {e.__class__.__name__}: {e})"
            ) from e

    async def query(self, query: str) -> dict:
        return await asyncio.to_thread(self.prometheus.custom_query, query=query)

    async def gather_data(
        self,
        object: K8sObjectData,
        resource: ResourceType,
        period: datetime.timedelta,
        step: datetime.timedelta = datetime.timedelta(minutes=30),
    ) -> ResourceHistoryData:
        """
        ResourceHistoryData: The gathered resource history data.
        """
        self.debug(f"Gathering data for {object} and {resource}")

        await self.add_historic_pods(object, period)

        MetricLoaderType = BaseMetricLoader.get_by_resource(resource)
        metric_loader = MetricLoaderType(self.config, self.prometheus)
        return await metric_loader.load_data(object, period, step, self.name())

    async def add_historic_pods(self, object: K8sObjectData, period: datetime.timedelta) -> None:
        """
        Finds pods that have been deleted but still have some metrics in Prometheus.
        Args:
            object (K8sObjectData): The Kubernetes object.
            period (datetime.timedelta): The time period for which to gather data.
        """

        days_literal = min(int(period.total_seconds()) // 60 // 24, 32)
        period_literal = f"{days_literal}d"
        pod_owners: list[str]
        pod_owner_kind: str

        if object.kind == "Deployment":
            replicasets = await self.query(
                "kube_replicaset_owner{"
                f'owner_name="{object.name}", '
                f'owner_kind="Deployment", '
                f'namespace="{object.namespace}"'
                "}"
                f"[{period_literal}]"
            )
            pod_owners = [replicaset["metric"]["replicaset"] for replicaset in replicasets]
            pod_owner_kind = "ReplicaSet"
        else:
            pod_owners = [object.name]
            pod_owner_kind = object.kind

        owners_regex = "|".join(pod_owners)
        related_pods = await self.query(
            "kube_pod_owner{"
            f'owner_name=~"{owners_regex}", '
            f'owner_kind="{pod_owner_kind}", '
            f'namespace="{object.namespace}"'
            "}"
            f"[{period_literal}]"
        )

        current_pods = set(object.pods)

        old_pods = [pod["metric"]["pod"] for pod in related_pods if pod["metric"]["pod"] not in current_pods]
        object.pods += old_pods
        object.deleted_pods_count = len(old_pods)
