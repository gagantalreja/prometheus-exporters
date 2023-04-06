import time
import json
import locale
import yaml
from math import ceil
import boto3
from datetime import datetime, timedelta
from argparse import ArgumentParser
from prometheus_client import Metric, REGISTRY, start_http_server


locale.setlocale(locale.LC_ALL, "")


class Constants:
    def __init__(self, config):
        self.scrape_interval = int(config.get("scrape_interval", 1)) * 86400
        self.port = int(config.get("port", 4298))

        self.project = config["project"]
        self.role_arn = config["role_arn"]
        self.region = config.get("region", "us-east-1")

        self.cost_type = config.get("cost_type", "AmortizedCost")
        self.cost_filter = json.loads(
            config.get(
                "filter",
                """{
                    "Not": {
                        "Dimensions": {
                            "Key": "RECORD_TYPE",
                            "Values": [
                                "Credit",
                                "Refund",
                                "Enterprise Discount Program Discount"
                            ],
                        }
                    }
                }""",
            )
        )

    @property
    def exporter_config(self):
        return {
            "scrape_interval": self.scrape_interval,
            "port": self.port,
            "project": self.project,
            "role_arn": self.role_arn,
            "region": self.region,
            "cost_type": self.cost_type,
            "filter": self.cost_filter,
        }

    def log_config(self):
        print(
            {
                "scrape_interval": self.scrape_interval,
                "port": self.port,
                "project": self.project,
                "role_arn": self.role_arn,
                "region": self.region,
                "cost_type": self.cost_type,
                "filter": self.cost_filter,
            }
        )


class AWSCostMetricCollector:
    def __init__(self, **kwargs):
        self.project = kwargs["project"]
        self.role_arn = kwargs["role_arn"]
        self.region = kwargs["region"]
        self.__sts = boto3.client("sts")

        self.__switch_to_client_account()
        self.__ce = boto3.client("ce")

        self.__time_range = self.__get_time_range()
        self.cost_type = kwargs["cost_type"]
        self.cost_filter = kwargs["cost_filter"]

    def __switch_to_client_account(self):
        acct_cred = self.__sts.assume_role(
            RoleArn=self.role_arn,
            RoleSessionName="inv_master",
        )

        ACCESS_KEY = acct_cred["Credentials"]["AccessKeyId"]
        SECRET_KEY = acct_cred["Credentials"]["SecretAccessKey"]
        SESSION_TOKEN = acct_cred["Credentials"]["SessionToken"]

        boto3.setup_default_session(
            region_name=self.region,
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            aws_session_token=SESSION_TOKEN,
        )

    def __get_time_range(self):
        now = datetime.utcnow()

        end = datetime(year=now.year, month=now.month, day=now.day)
        start = end - timedelta(days=1)

        start = start.strftime("%Y-%m-%d")
        end = end.strftime("%Y-%m-%d")

        return {"Start": start, "End": end}

    def __get_aws_cost(self):
        resp = self.__ce.get_cost_and_usage(
            TimePeriod=self.__time_range,
            Granularity="DAILY",
            Metrics=[self.cost_type],
            Filter=self.cost_filter,
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )

        cost_as_per_service = {"Cost": dict(), "Total": 0.0}

        for r in resp["ResultsByTime"][0]["Groups"]:
            cost = ceil(float(r["Metrics"]["AmortizedCost"]["Amount"]))
            service = "_".join(r["Keys"][0].split())
            cost_as_per_service["Cost"][service] = cost
            cost_as_per_service["Total"] += float(
                r["Metrics"]["AmortizedCost"]["Amount"]
            )

        print(self.__time_range, cost_as_per_service)

        return cost_as_per_service

    def collect(self):
        metric = Metric(
            "aws_project_cost_as_per_service",
            "Service wise cost for an AWS account",
            "gauge",
        )
        costs = self.__get_aws_cost()

        for service, cost in costs["Cost"].items():
            metric.add_sample(
                f"aws_cost",
                value=cost,
                labels={
                    "project": self.project,
                    "service": service,
                    "type": "individual_service",
                },
            )

        metric.add_sample(
            f"aws_cost",
            value=costs["Total"],
            labels={"project": self.project, "type": "daily_total"},
        )

        yield metric


def __init__():
    parser = ArgumentParser()

    parser.add_argument("-f", "--filepath", help="exporter config file path")

    return parser.parse_args()


if __name__ == "__main__":

    args = __init__()
    loaded_config = {}

    if args.filepath:
        print("Reading config file")
        with open(args.filepath, "r") as config:
            loaded_config = yaml.safe_load(config.read())

    config = Constants(config=loaded_config)
    config.log_config()

    start_http_server(config.exporter_config["port"])
    print(f"Started server on port {config.exporter_config['port']}")

    metrics = AWSCostMetricCollector(
        project=config.exporter_config["project"],
        role_arn=config.exporter_config["role_arn"],
        cost_filter=config.exporter_config["filter"],
        cost_type=config.exporter_config["cost_type"],
        region=config.exporter_config["region"],
    )

    REGISTRY.register(metrics)
    while True:
        time.sleep(1)
