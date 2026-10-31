# dendra-exporter

`dendra-exporter` turns the reports of the miner-health watch into metrics for Prometheus, so the
miner shows on the same screens as the rest of the machine. It reads files and runs nothing.

## What it reads

The watch (`deploy/testnet-miner/miner_health.sh`), run on a schedule by the crontab line the kit
installs, writes one JSON document per miner identity after every run: `miner-health.last.json`.
A machine with one card has one identity, slot 0, and one file in the kit folder (`deploy/testnet-miner`
of the clone). A machine with several cards runs one identity per card: the file of slot 0 stays in the
kit folder, and the file of slot k is `<kit folder>/gpu/<k>/miner-health.last.json`.

The files are read again at each scrape, so a new report shows at the next scrape. A file larger
than 1 MiB is refused. A field that a report leaves out, writes as `null`, or writes with the
wrong type is not a zero: its series is left out.

A file that cannot be read (missing, too large, not JSON) serves no slot series, because its slot
number cannot be read; it raises `dendra_miner_health_read_errors_total` for that file. When two files
name the same slot, the first one given wins and the other is logged.

## Build

    cd chain && go build ./cmd/dendra-exporter

## Run

With the clone at its default place, the kit folder is:

    KIT=$HOME/dendra-network/deploy/testnet-miner

One card:

    dendra-exporter -file "$KIT/miner-health.last.json"

Several cards, one identity per card:

    dendra-exporter \
      -file "$KIT/miner-health.last.json" \
      -glob "$KIT/gpu/*/miner-health.last.json"

Both flags repeat. The patterns of `-glob` are matched again at each scrape, so the report of a
new card shows by itself. The metrics are served at `http://127.0.0.1:9478/metrics`; the address
changes with `-listen`. The exporter listens on the loopback address by default: Prometheus, on
the same machine, reads it there. The metrics name the report files that failed to read, so open
a wider address only on a network you control. Every other path answers 404.

## The Prometheus job

    scrape_configs:
      - job_name: dendra-miner
        scrape_interval: 1m
        static_configs:
          - targets: ['127.0.0.1:9478']

## The metrics

Every miner metric carries the `slot` it describes.

| Metric | Type | Value |
|---|---|---|
| `dendra_miner_health_up` | gauge | 1 when the report of the slot was read and parsed |
| `dendra_miner_health_rc` | gauge | 0 ok, 1 a problem is standing, 2 not everything measured |
| `dendra_miner_health_checks` | gauge | the counts of the report summary, label `state` |
| `dendra_miner_health_check` | gauge | one check of the host list, labels `id` and `name`: 0 ok, 1 ko, 2 unmeasured |
| `dendra_miner_health_generated_timestamp_seconds` | gauge | when the report was generated |
| `dendra_miner_health_schedule_period_seconds` | gauge | the period of the kit schedule |
| `dendra_miner_health_selftest_rc` | gauge | the rc of the self-test, when the report holds it |
| `dendra_miner_health_judge_role` | gauge | 1 for the label `word`, when the report holds a judge role |
| `dendra_miner_health_read_errors_total` | counter | failed reads since the start, label `file` |

The age of a report is left to Prometheus: `time() - dendra_miner_health_generated_timestamp_seconds`.

## The dashboard

`deploy/testnet-miner/grafana/dendra-miner-health.json` reads these metrics. In Grafana:
Dashboards, New, Import, upload the file. The data source is the Prometheus selector at the top of
the dashboard, and the slot selector next to it picks one card or all of them. The panels: the
verdict per slot, the counts ok, ko and unmeasured, the checks not ok as a table, the age of the
last report against two schedule periods, and the judge role.
