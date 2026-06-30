.PHONY: test venv install

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

venv:
	python3 -m venv $(VENV)

install: venv
	$(PIP) install -e ".[test]"

# Run from the repo root. The suite is collected with rootdir pinned to tests/
# (see tests/pytest.ini) because the repo root IS the hermes_push package — its
# __init__.py cannot be imported standalone.
test:
	$(PY) -m pytest tests/ -q
