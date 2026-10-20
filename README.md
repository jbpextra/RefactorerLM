# RefactorerLLM

RefactorerLLM is a research prototype that uses AI to suggest improvements to the
structure of Python software. It finds potential design problems, proposes changes,
checks them, and submits the accepted changes for human review on GitHub.

This project is part of a **bachelor's thesis for the Degree in Informatics
Engineering at the Universitat Politècnica de Catalunya (UPC), Facultat
d'Informàtica de Barcelona (FIB)**. It explores whether automated agents can help
reduce technical debt: design choices that make software harder to maintain over
time.

## What problem does it address?

As software grows, the relationships between its parts can become difficult to
understand and change. These warning signs are called **architectural smells**.
A smell suggests a possible design problem; it does not necessarily mean the
software is broken.

RefactorerLLM currently supports two types:

| Smell | What it means | Example improvement |
| --- | --- | --- |
| **Cyclic Dependency** | Parts of the program depend on each other in a loop. | Move shared functionality into a separate module to break the loop. |
| **Potential Redundant Abstractions** | Classes or layers have overlapping responsibilities or add little value. | Combine related implementations or remove unnecessary wrappers. |

The detector can report other smells, but the tool only selects these two types
for refactoring. **Refactoring** means reorganizing code with the aim of preserving
what the software does.

## How it works

1. **Detect:** analyze the Python code with `code-quality-analyzer`.
2. **Select:** choose supported findings using the project's configuration and
   skip findings already recorded as attempted.
3. **Plan and edit:** ask a large language model (LLM) to explain an approach, then
   generate the file changes.
4. **Check:** run available tests, check selected structural rules, look for
   references to deleted modules, and compare the original and modified code.
5. **Propose:** if the checks accept the changes and there is a code difference,
   open a **pull request**: a proposed change that a maintainer can review.

The pull request includes the detected problem, the proposed solution, the files
changed, and the validation results. The workflow does not merge it automatically.

### What the checks can and cannot establish

The checks help catch problems, but they do not prove that a refactoring preserves
behavior or resolves the original smell. The workflow does not rerun the detector
after editing.

- **Tests:** failing tests reject a change. If `pytest` is missing or finds no
  tests, this check is marked as skipped and the change can still be accepted.
  Such pull requests receive a `no-tests` label.
- **Structural checks:** selected refactoring strategies have additional rules,
  and deleted modules are checked for remaining import references.
- **Code similarity:** CodeBERTScore estimates how similar the code is before and
  after editing. The default threshold is `0.75`, configurable through the
  `SIMILARITY_THRESHOLD` environment variable. Similar code can still contain
  bugs, so a high score is not proof of correctness.

Each validation run writes `validation_assessment.json`, recording the decision
and which checks passed, failed, were skipped, did not apply, or were not reached.
Human review and meaningful project tests remain essential.

## Use it with GitHub Actions

The main way to use RefactorerLLM is through its
[reusable GitHub Actions workflow](.github/workflows/refactorer.yml), which runs
inside the Python repository you want to improve.

1. Copy [refactor_configuration.yml](refactor_configuration.yml) into that
   repository and adjust it as needed.
2. Add an `OPENAI_API_KEY` repository secret with access to the configured models.
3. Allow GitHub Actions to write repository contents and create pull requests.
4. Add the following file as `.github/workflows/refactor.yml`, then run it from
   the repository's **Actions** tab.

```yaml
name: Run Refactorer

on:
  workflow_dispatch:

jobs:
  refactor:
    uses: jbpextra/RefactorerLM/.github/workflows/refactorer.yml@main
    permissions:
      contents: write
      pull-requests: write
    with:
      python_version: "3.13"
      config_file: refactor_configuration.yml
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

Planning and editing send source code to the configured model service and may
incur API costs. The workflow installs Refactorer's dependencies, but currently
does not install `pytest` or the target project's own dependencies. Enabling
project tests therefore requires adapting the refactoring job to install them.

If the default GitHub token cannot read the Refactorer repository, also pass a
`REFACTORER_REPOSITORY_TOKEN` secret with access to it.

### Choose what to process

The supplied configuration analyzes the repository root, selects the two supported
smells at **High** or **Medium** severity, and limits each run to **five proposals**.
Change `target_directory` to analyze a subdirectory such as `src`, or adjust
`smell_filter` and `max_proposal_limit`. A limit of `0` means no limit.

The workflow creates a branch named `refactor/<UUID>` for each attempt, where the
UUID is a stable identifier for the finding. An existing remote branch causes that
finding to be skipped on later runs, including when the earlier attempt failed or
produced no changes.

Common workflow inputs:

| Input | Purpose | Default |
| --- | --- | --- |
| `python_version` | Python version for the jobs | `3.13` |
| `config_file` | Selection configuration in the target repository | `refactor_configuration.yml` |
| `target_directory` | Directory to analyze and edit | Configuration value, otherwise `.` |
| `base_branch` | Branch the pull request targets | Repository's default branch |
| `max_parallel` | Number of findings processed at once | `2` |
| `refactorer_ref` | Version of Refactorer's helper scripts | `main` for reusable workflow calls |

If you replace `@main` in the example with a tag, branch, or commit, set
`refactorer_ref` to the same value. The workflow file documents all inputs and its
outputs: `smells-detected`, `smell-uuids`, and `target-directory`.

## Local development

Use **Python 3.11 or newer** and Git. From a checkout of this repository, create a
virtual environment and install the tool and development dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

These commands use a macOS/Linux shell. On Windows PowerShell, activate the
environment with `.venv\Scripts\Activate.ps1`.

Installation includes the smell detector. Run Refactorer's own tests with:

```bash
python -m pytest -q
```

The tests use sample files and replace external model calls with test responses.
They do not require network access or API keys.

Tests are organized by scope:

- `tests/unit/`: focused helpers, parsing, validation rules, and mocked client behavior.
- `tests/integration/`: stage execution, artifact contracts, Git and SDK integration,
  workflow wiring, and the pipeline smoke test.
- `tests/fixtures/` and `tests/conftest.py`: sample data and fixtures shared by both suites.

Run either suite independently:

```bash
python -m pytest tests/unit -q
python -m pytest tests/integration -q
```

### Run individual stages

The examples below assume you are in the root of the Git repository to analyze,
with both [refactor_configuration.yml](refactor_configuration.yml) and
[code_quality_config.yaml](code_quality_config.yaml) copied there if needed.
Keep the virtual environment active and install the target project's test
dependencies. Use a clean working branch: the application step edits files
directly, and a rejected change is not automatically undone.

First, detect problems and select candidates. These steps do not call an LLM:

```bash
analyze_code_quality . --type architectural \
    --config code_quality_config.yaml --output report.csv --debug

python -P -m scripts.filter_smells \
    --report-file report.csv \
    --config refactor_configuration.yml \
    --output-file smells_to_process.json \
    --existing-branches ""
```

Here, `--existing-branches ""` supplies no previous-attempt history. The GitHub
workflow supplies its remote branch list instead.

Set `OPENAI_API_KEY` in your environment before planning or editing. Choose an
identifier from `smells_to_process.json` and replace the example value below.
If the list is empty, no findings were selected.

```bash
SMELL_UUID="replace-with-an-id-from-smells_to_process.json"

python -P -m scripts.generate_proposal \
    --smell-uuid "$SMELL_UUID" \
    --report-file filtered_report.csv
```

Read `proposal.json` to inspect the plan, then apply it and run the checks before
committing the changes. Validation uses Git's list of uncommitted changes:

```bash
python -P -m scripts.apply_refactoring \
    --smell-uuid "$SMELL_UUID" \
    --proposal-file proposal.json \
    --modules-file modules.json

python -P -m scripts.correctness_filter
```

Inspect the code changes and `validation_assessment.json` afterward. These local
commands do not create a pull request.

For a nested codebase, run **all stages from the same analysis directory**, such
as `src`, and adjust the configuration paths accordingly. The local commands use
their working directory; they do not change directories based on the config's
`target_directory`. The `-P` flag avoids adding the working directory to Python's
module search path, helping keep the tool's `scripts` package separate from the
target project's packages.

### Optional: local workflow execution

The repository includes [configuration for `act`](.actrc) to run GitHub Actions
locally. This advanced setup also needs a container runtime, API and GitHub
credentials, and `ACT_HOST_IP` configured for the local artifact server.
It executes the real workflow and may call paid APIs, push branches, and open
pull requests. Workflow artifacts are stored in `.artifacts`.

## Model settings

The code defaults to `gpt-5.6-luna` for both planning and editing. You can configure
each stage separately using environment variables locally or workflow inputs:

| Environment variable | Workflow input | Local default | Workflow default |
| --- | --- | --- | --- |
| `OPENAI_BASE_URL` | `openai_base_url` | SDK default | `https://api.openai.com/v1` |
| `REASONER_MODEL` | `reasoner_model` | `gpt-5.6-luna` | `gpt-5.6-luna` |
| `CODER_MODEL` | `coder_model` | `gpt-5.6-luna` | `gpt-5.6-luna` |
| `REASONER_REASONING_EFFORT` | `reasoner_reasoning_effort` | `xhigh` | `xhigh` |
| `CODER_REASONING_EFFORT` | `coder_reasoning_effort` | `low` | `none` |

The reasoning-effort settings control the effort requested from the model. Empty
or unset model and effort variables use the local defaults; explicitly setting
the workflow's `coder_reasoning_effort` to `""` therefore selects `low`.

Custom API endpoints must support the Responses API, JSON schema output, and
function calling used by [scripts/llm_clients.py](scripts/llm_clients.py). Use a
base URL including `/v1`, without `/responses`. `OPENAI_API_KEY` is required for
planning and editing.

## Files and results

Each stage saves files so its inputs, decisions, and results can be inspected:

| Stage | Main output files |
| --- | --- |
| Detection | `report.csv` |
| Selection | `filtered_report.csv`, `smells_to_process.json`, `smells_detected.txt` |
| Planning | `proposal.json`, `modules.json` (original source), `prompt.txt` |
| Application | Edited source files and `refactored_files.json` |
| Validation | `validation_assessment.json` |
| Pull request description | `pr_title.txt`, `pr_body.txt` |

The GitHub workflow also produces a run summary and downloadable reports and
refactoring artifacts.

- [scripts/](scripts/): detection-report processing, AI integration, file editing,
  validation, and pull request preparation.
- [.github/workflows/refactorer.yml](.github/workflows/refactorer.yml): orchestration
  of the complete process.
- [refactor_configuration.yml](refactor_configuration.yml): selection rules and
  analysis directory.
- [code_quality_config.yaml](code_quality_config.yaml): detector thresholds.
- [tests/](tests/): automated tests and sample projects.

## License

RefactorerLLM is released under the
[GNU Affero General Public License v3.0](LICENSE).
