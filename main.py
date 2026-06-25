"""Runner script for DFOptimizer service."""

import argparse
import logging
import logging.config
import os
import sys

import structlog


def configure_logging(level: str = "info"):
    pre_chain = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_logger_name,
    ]

    logging_config = {
        "version": 1,
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": level.upper(),
                "formatter": "json",
            },
        },
        "formatters": {
            "json": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processor": structlog.processors.JSONRenderer(),
                "foreign_pre_chain": pre_chain,
            },
        },
        "loggers": {
            "": {
                "handlers": ["console"],
                "level": level.upper(),
                "propagate": False,
            }
        },
    }

    logging.config.dictConfig(logging_config)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_logger_name,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def main():
    parser = argparse.ArgumentParser(description="DFOptimizer streaming service")
    parser.add_argument("--group-file", default=os.environ.get("MOFKA_GROUP_FILE", "mofka.group.json"))
    parser.add_argument("--input-topic", default=os.environ.get("DFOPTIMIZER_INPUT_TOPIC", "diagnosis.findings"))
    parser.add_argument("--output-topic", default=os.environ.get("DFOPTIMIZER_OUTPUT_TOPIC", "optimizer.plans"))
    parser.add_argument("--registry-topic", default=os.environ.get("DFOPTIMIZER_REGISTRY_TOPIC", "optimizer.registry"))
    parser.add_argument("--idle-timeout", type=int, default=int(os.environ.get("DFOPTIMIZER_IDLE_TIMEOUT_SEC", "0")))
    parser.add_argument("--pull-timeout-ms", type=int, default=int(os.environ.get("DFOPTIMIZER_PULL_TIMEOUT_MS", "1000")))
    parser.add_argument("--consumer-name", default=os.environ.get("DFOPTIMIZER_CONSUMER_NAME", ""))
    parser.add_argument("--debug", action="store_true", default=os.environ.get("DFOPTIMIZER_DEBUG", "") == "1")
    parser.add_argument("--no-registry", action="store_true",
                        help="Skip knob registry consumer (for global optimizer that has no knob defs)")
    parser.add_argument("--global-group-file", default="",
                        help="Group file for global infra bedrock (ofi+cxi). Enables global plan relay.")
    parser.add_argument("--global-input-topic", default="global_plans",
                        help="Topic on global bedrock to consume plans from")
    parser.add_argument(
        "--relay-only",
        action="store_true",
        default=os.environ.get("DFOPTIMIZER_RELAY_ONLY", "") == "1",
        help="Skip local finding consumption and only relay global plans locally",
    )
    # Transport selection: mofka (default, LiveFlow), zmq (local liveflow chain),
    # or file (offline replay of saved findings).
    parser.add_argument("--transport", choices=["mofka", "zmq", "file"],
                        default=os.environ.get("DFOPTIMIZER_TRANSPORT", "mofka"))
    parser.add_argument("--address", default=os.environ.get("DFOPTIMIZER_ZMQ_FINDINGS", ""),
                        help="ZMQ endpoint to consume findings from (transport=zmq)")
    parser.add_argument("--bind", action="store_true",
                        help="ZMQ: bind the findings socket instead of connecting (transport=zmq)")
    parser.add_argument("--plans-address", default=os.environ.get("DFOPTIMIZER_ZMQ_PLANS", ""),
                        help="ZMQ endpoint to publish ActionPlans to (transport=zmq)")
    parser.add_argument("--findings-file", default="",
                        help="Findings JSONL/array to replay (transport=file)")
    args = parser.parse_args()

    configure_logging(level="debug" if args.debug else "info")
    logger = structlog.get_logger()

    from dfoptimizer.optimizer import Optimizer

    logger.info(
        "optimizer.start",
        input_topic=args.input_topic,
        output_topic=args.output_topic,
        registry_topic=args.registry_topic,
    )

    optimizer = Optimizer()

    if args.transport == "file":
        optimizer.run_file(args.findings_file)
    elif args.transport == "zmq":
        optimizer.run_zmq(
            address=args.address,
            bind=args.bind,
            output_address=args.plans_address,
            idle_timeout_sec=float(args.idle_timeout or 10),
            poll_timeout_ms=args.pull_timeout_ms,
        )
    else:  # mofka (default; unchanged)
        optimizer.run_mofka(
            group_file=args.group_file,
            input_topic=args.input_topic,
            output_topic=args.output_topic,
            registry_topic=args.registry_topic,
            consumer_name=args.consumer_name,
            idle_timeout_sec=args.idle_timeout,
            pull_timeout_ms=args.pull_timeout_ms,
            no_registry=args.no_registry,
            global_group_file=args.global_group_file,
            global_input_topic=args.global_input_topic,
            relay_only=args.relay_only,
        )


if __name__ == "__main__":
    main()
