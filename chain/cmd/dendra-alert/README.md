# dendra-alert

`dendra-alert` sends the owner's alerts to a private chat channel through a webhook. It runs on
the chain host from cron, one pass every few minutes. It reads the node and the services on that
host and posts a message when something needs the owner: the block height stops, the node process
grows past a memory limit, nodes are still below the kit floor shortly before its deadline, a
ranked day was left without a grade, or the season payout cron raised its alert or went silent.

An alert is posted once when it starts, again when its content changes or after the reminder
delay, and once more when it ends. What the last pass saw lives in `state.json` under the state
directory, beside the webhook file, which is read without a default and never printed.

## Build

```bash
cd chain && go build ./cmd/dendra-alert
```

## Sub-commands

```
dendra-alert run      one pass: read, compare with the last pass, post what changed
dendra-alert status   read everything and print it; posts nothing and writes no state
dendra-alert baseline print the ranked days with no grade that the public status lists now,
                      as ungraded_ranked_days=0,1,6 (nothing is written; install stores the list
                      as the accepted days)
dendra-alert test     post one test message
dendra-alert help     (also -h, --help)
```

## The checks

| Name | Fires when |
|---|---|
| `blocks` | the height read from the node's `/status` has not changed for `DENDRA_ALERT_STALL_S` seconds |
| `memory` | no container runs for the chain service, or `dendrad` holds resident memory plus swap above `DENDRA_ALERT_CHAIN_MEM_MIB` |
| `kit` | inside the lead window before `DENDRA_KIT_DEADLINE`, more proven live nodes than tolerated still declare a kit below `DENDRA_KIT_MIN` |
| `grading` | a ranked day with sampled answers has no grade, and the owner did not accept it |
| `payout` | the season payout cron raised its alert file, or wrote nothing to its log for `DENDRA_ALERT_PAYOUT_SILENT_H` hours |

A check that cannot read its source does not fire at once: after `DENDRA_ALERT_UNKNOWN_PASSES`
failed passes in a row it raises an unreadable alert. A check named in `DENDRA_ALERT_SKIP_CHECKS`
no longer speaks, and the records it raised go without a message.

## Settings

A value comes from the environment first, then from the file `settings` of the state directory
(`NAME=VALUE` lines; a name that is not a setting is an error, so a typo is never taken for a
value), then from the default below. `DENDRA_ALERT_DIR` names that directory, so it cannot be set
in the file. `{home}` in a default stands for `$HOME`, which must be set
to an absolute path.

| Name | Default | Role |
|---|---|---|
| `DENDRA_ALERT_DIR` | `{home}/.dendra-alert` | state directory: webhook, settings, state.json, alert.log, run.lock |
| `DENDRA_ALERT_RPC_URL` | `http://127.0.0.1:26657` | RPC of the chain node on this host |
| `DENDRA_ALERT_CAPACITY_URL` | `http://127.0.0.1:8092/capacity` | capacity registry on this host (`verified.kit_versions`) |
| `DENDRA_ALERT_PROGRAMME_URL` | `https://testnet-api.dendranetwork.com/final-season/v1` | season programme API, as the grading cron reads it (`status.ungraded_ranked_days`) |
| `DENDRA_ALERT_ENV_FILE` | `{home}/dendra/.env` | compose `.env` that carries `DENDRA_KIT_MIN` and `DENDRA_KIT_DEADLINE` |
| `DENDRA_ALERT_PAYOUT_DIR` | `{home}/.dendra-season-payout` | directory of the season payout cron (season-payout.alert, season-payout.log) |
| `DENDRA_ALERT_COMPOSE_PROJECT` | `dendra` | compose project name of the chain host |
| `DENDRA_ALERT_CHAIN_SERVICE` | `chain` | compose service name of the node |
| `DENDRA_ALERT_STALL_S` | `900` | seconds the block height may stay still before the blocks alert fires |
| `DENDRA_ALERT_CHAIN_MEM_MIB` | `2400` | MiB of resident memory plus swap of `dendrad` above which the memory alert fires |
| `DENDRA_ALERT_KIT_LEAD_H` | `72` | hours before `DENDRA_KIT_DEADLINE` from which the adoption check runs |
| `DENDRA_ALERT_KIT_BELOW_MAX` | `0` | verified live nodes below `DENDRA_KIT_MIN` tolerated before the adoption alert fires |
| `DENDRA_ALERT_PAYOUT_SILENT_H` | `7` | hours without a line in season-payout.log before the payout cron counts as silent |
| `DENDRA_ALERT_ACCEPTED_UNGRADED_DAYS` | (empty) | ranked days with no grade that the owner accepted, separated by commas or spaces |
| `DENDRA_ALERT_UNKNOWN_PASSES` | `3` | consecutive passes a check may fail to read before an unreadable alert fires |
| `DENDRA_ALERT_REPEAT_H` | `12` | hours between two reminders of an alert that stays firing |
| `DENDRA_ALERT_SKIP_CHECKS` | (empty) | checks to leave out, separated by commas: blocks, memory, kit, grading, payout |
| `DENDRA_ALERT_READ_TIMEOUT_S` | `20` | seconds a read of the node, a service or docker may take |

## The state directory

Everything the command keeps lives under `DENDRA_ALERT_DIR`: the `webhook` file, the `settings`
file, `state.json` (what the last pass saw), `alert.log` (rotated at 1 MiB) and `run.lock`. A
second pass that finds the lock held stops without posting; a `state.json` that does not parse is
set aside and the pass starts from an empty state, so a damaged file cannot stop every later
alert.

The webhook is read from the file `webhook` of the state directory and from nowhere else: mode
600, one URL alone on one line, an https chat webhook URL (a redirect is not followed, and the
value never appears in a message or an error).

## Exit codes

- 0: the command did its work. For `run`, the pass finished, whether alerts are firing or not, or
  another pass holds the lock.
- 1: `run` or `test` could not deliver a message; `status` found an alert firing, a check it could
  not read, a state it could not read or a webhook file that is not ready; `baseline` could not
  read the public status.
- 2: usage, or the configuration is not ready; for `run` and `test`, also the webhook file or the
  state directory, and for `run` a state that could not be read or saved.
