from typing import List

from dagster import (
    In,
    Nothing,
    Out,
    ResourceDefinition,
    RetryPolicy,
    RunRequest,
    ScheduleDefinition,
    SkipReason,
    graph,
    op,
    sensor,
    static_partitioned_config,
)
from project.resources import mock_s3_resource, redis_resource, s3_resource
from project.sensors import get_s3_keys
from project.types import Aggregation, Stock


@op(
    config_schema={"s3_key": str},
    required_resource_keys={"s3"},
    out={"stocks": Out(dagster_type=List[Stock])},
    tags={"kind": "s3"},
    description="Get a list of stocks from an S3 file",
)
def get_s3_data(context):
    stocks = context.resources.s3.get_data(context.op_config["s3_key"])
    output = list()
    for record in stocks:
        stock = Stock.from_list(record)
        output.append(stock)
    return output


@op(
    ins={"stocks": In(dagster_type=List[Stock])},
    out={"agg_result": Out(dagster_type=Aggregation)},
    description="Given a list of stocks return the stock with the largest high"
)
def process_data(stocks) -> Aggregation:
    max_high = max(stocks, key=lambda stock: stock.high)
    return Aggregation(date=max_high.date, high=max_high.high)


# noinspection PyTypeChecker
@op(
    ins={"agg_result": In(dagster_type=Aggregation)},
    required_resource_keys={"redis"},
    tags={"kind": "redis"},
    description="Store aggregation results in redis"
)
def put_redis_data(context, agg_result) -> Nothing:
    context.resources.redis.put_data(str(agg_result.date), str(agg_result.high))


@graph
def week_3_pipeline():
    put_redis_data(process_data(get_s3_data()))


local = {
    "ops": {"get_s3_data": {"config": {"s3_key": "prefix/stock_9.csv"}}},
}

docker = {
    "resources": {
        "s3": {
            "config": {
                "bucket": "dagster",
                "access_key": "test",
                "secret_key": "test",
                "endpoint_url": "http://localstack:4566",
            }
        },
        "redis": {
            "config": {
                "host": "redis",
                "port": 6379,
            }
        },
    },
    "ops": {"get_s3_data": {"config": {"s3_key": "prefix/stock_9.csv"}}},
}


@static_partitioned_config(partition_keys=[str(num) for num in range(1, 11)])
def docker_config(partition_key: str):
    return {
        "resources": {
            "s3": {
                "config": {
                    "bucket": "dagster",
                    "access_key": "test",
                    "secret_key": "test",
                    "endpoint_url": "http://host.docker.internal:4566",
                }
            },
            "redis": {
                "config": {
                    "host": "redis",
                    "port": 6379,
                }
            },
        },
        "ops": {"get_s3_data": {"config": {"s3_key": f"prefix/stock_{partition_key}.csv"}}},
}


local_week_3_pipeline = week_3_pipeline.to_job(
    name="local_week_3_pipeline",
    config=local,
    resource_defs={
        "s3": mock_s3_resource,
        "redis": ResourceDefinition.mock_resource(),
    },
)

docker_week_3_pipeline = week_3_pipeline.to_job(
    name="docker_week_3_pipeline",
    config=docker_config,
    resource_defs={
        "s3": s3_resource,
        "redis": redis_resource,
    },
    op_retry_policy=RetryPolicy(max_retries=10, delay=1)
)

local_week_3_schedule = ScheduleDefinition(job=local_week_3_pipeline, cron_schedule="*/15 * * * *")

docker_week_3_schedule = ScheduleDefinition(job=docker_week_3_pipeline, cron_schedule="0 * * * *")


@sensor(job=docker_week_3_pipeline, minimum_interval_seconds=30)
def docker_week_3_sensor(context):
    new_keys = get_s3_keys(
        bucket="dagster",
        prefix="prefix",
        endpoint_url="http://host.docker.internal:4566",
        since_key=None
    )
    if not new_keys:
        yield SkipReason("No new s3 files found in bucket.")
        return
    for key in new_keys:
        yield RunRequest(
            run_key=key,
            run_config={
                "resources": {
                    "s3": {
                        "config": {
                            "bucket": "dagster",
                            "access_key": "test",
                            "secret_key": "test",
                            "endpoint_url": "http://host.docker.internal:4566",
                        }
                    },
                    "redis": {
                        "config": {
                            "host": "redis",
                            "port": 6379,
                        }
                    },
                },
                "ops": {"get_s3_data": {"config": {"s3_key": key}}},
            }
        )
