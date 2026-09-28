# LoanLens - common tasks. `make setup && make all` runs everything locally.
PY      ?= .venv/bin/python
LL      ?= .venv/bin/loanlens
PROFILE ?= freddie
SITE     = $(shell $(PY) -c "import sysconfig; print(sysconfig.get_paths()['purelib'])" 2>/dev/null)
export LOANLENS_PROFILE = $(PROFILE)

.PHONY: setup fix-libomp-macos fetch synth ingest transform train survival stress decide fairness report \
        all status test test-fast lint dagster dbt-docs docker-build docker-all postgres publish clean

setup:                 ## create .venv and install everything
	uv venv --python 3.11 .venv
	uv pip install --python .venv -e ".[dev,orchestration,postgres]"
	$(MAKE) fix-libomp-macos

# XGBoost wheels on macOS need libomp (normally `brew install libomp`). Without Homebrew,
# point xgboost at the copy bundled with scikit-learn.
fix-libomp-macos:
	@if [ "$$(uname)" = "Darwin" ] && [ ! -e /opt/homebrew/opt/libomp/lib/libomp.dylib ] && [ ! -e /usr/local/opt/libomp/lib/libomp.dylib ]; then \
	  install_name_tool -add_rpath "@loader_path/../../sklearn/.dylibs" $(SITE)/xgboost/lib/libxgboost.dylib 2>/dev/null || true; \
	  codesign --force -s - $(SITE)/xgboost/lib/libxgboost.dylib 2>/dev/null || true; \
	  echo "xgboost linked to scikit-learn's libomp"; fi

fetch:     ; $(LL) fetch-fred
synth:     ; $(LL) synth
ingest:    ; $(LL) ingest
transform: ; $(LL) transform
train:     ; $(LL) train
survival:  ; $(LL) survival
stress:    ; $(LL) stress
decide:    ; $(LL) decide
fairness:  ; $(LL) fairness
report:    ; $(LL) report
status:    ; $(LL) status
all:       ; $(LL) all

test:      ; $(PY) -m pytest -q
test-fast: ; $(PY) -m pytest -q -m "not slow"
lint:      ; .venv/bin/ruff check src tests

dagster:   ; .venv/bin/dagster dev -m loanlens.orchestration.definitions
dbt-docs:
	LOANLENS_WAREHOUSE=$(PWD)/data/warehouse/loanlens.duckdb .venv/bin/dbt docs generate --project-dir dbt --profiles-dir dbt
	LOANLENS_WAREHOUSE=$(PWD)/data/warehouse/loanlens.duckdb .venv/bin/dbt docs serve --project-dir dbt --profiles-dir dbt

docker-build: ; docker compose build
docker-all:   ; docker compose run --rm pipeline
postgres:     ; docker compose up -d postgres
publish:      ; LOANLENS_POSTGRES_DSN=postgresql://loanlens:loanlens@localhost:5432/loanlens $(LL) publish-postgres

clean:        ## remove generated data and artifacts (keeps the Freddie Mac files and the FRED cache)
	rm -rf data/warehouse data/alerts artifacts powerbi/data dbt/target dbt/logs
