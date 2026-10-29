# Local checks without GitHub Actions

Ermik runs commands from `checks` in `config.json`. Each command is an argument array executed in the selected Docker image with the checkout at `/work`. Checks run without network access, so install compilers, libraries, and dependencies in the image ahead of time. Commands come from trusted local configuration, not issue text.

```json
{
  "image": "your-project-checks@sha256:REPLACE_WITH_REAL_DIGEST",
  "checks": [
    ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
    ["python3", "-m", "compileall", "-q", "src"]
  ]
}
```

This is a configuration fragment, not a complete `config.json`. For another language, supply its build and test commands. A check must exit nonzero on failure and detect the expected number of tests: a command that exits successfully without running tests does not establish the acceptance criteria. If the project needs an integration database, network access, or gameplay or user scenarios, use a separate test environment and manual acceptance criteria. They are not included automatically in network-isolated `checks`.

For JUnit XML, the OpenCode image includes `python3 -m ermik.ci`. Configure a trusted command that produces and immediately validates the report. For example, when the image contains `pytest` and the project dependencies:

```json
"checks": [["sh", "-c", "PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests --junitxml=/tmp/ermik-tests.xml && python3 -m ermik.ci /tmp/ermik-tests.xml --min-tests 2"]]
```

This replaces the example `checks` field. `/tmp` exists only inside that check's container, so create and read the report in one command. The validator accepts a `testsuite` or `testsuites` root, counts `testcase` elements, requires at least one test by default, and rejects `failure` or `error`. It also rejects missing, symbolic, invalid, or over 4 MiB reports. Set the minimum to the number your project expects. For a different image, include `ermik/ci.py` and Python or use an equivalent validator. The test command itself must also exit nonzero on failure.

| Stage | Check | Failure outcome |
| --- | --- | --- |
| Before publication | All `checks` on the exact branch commit | Issue is blocked; no draft PR is created |
| Local review | The same `checks` on the fetched exact PR HEAD | No review record; merge is unavailable |
| After merge | The same `checks` on the exact new `main` | Failure is reported; fix through a new PR |

A passing record contains the HEAD SHA, time, image identifier, commands, zero exit status, and SHA-256 of each private log. Logs stay under `state_dir/ci-logs/`, outside Git. Review again after any new HEAD; the previous record applies only to its original commit. Inspect the full diff, PR metadata, and issue criteria before merge. Passing commands do not replace a substantive review.

GitHub Actions can be added to the target project but is not required for this workflow. Local Docker checks should have bounded time, memory, and concurrency through configuration and Docker.
