VENV ?= .venv
PYTHON := $(VENV)/bin/python
PIP := $(PYTHON) -m pip

.PHONY: dev-install lint format typecheck test check req-compile req-upgrade req-sync ai-fix

dev-install:
	$(PIP) install -r requirements.txt -r requirements_mcp.txt -r requirements_webapi.txt -r requirements-dev.txt

lint:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .

typecheck:
	$(PYTHON) -m pyright

test:
	@if find . -maxdepth 3 -type f \( -name "test_*.py" -o -name "*_test.py" \) | grep -q .; then \
		$(PYTHON) -m pytest -q; \
	else \
		echo "No tests found. Skipping pytest."; \
	fi

check: lint typecheck test

req-compile:
	$(PIP) install pip-tools
	$(PYTHON) -m piptools compile -o requirements.txt requirements.in
	$(PYTHON) -m piptools compile -o requirements_mcp.txt requirements_mcp.in
	$(PYTHON) -m piptools compile -o requirements_webapi.txt requirements_webapi.in
	$(PYTHON) -m piptools compile -o requirements-dev.txt requirements-dev.in

req-upgrade:
	$(PIP) install pip-tools
	$(PYTHON) -m piptools compile --upgrade -o requirements.txt requirements.in
	$(PYTHON) -m piptools compile --upgrade -o requirements_mcp.txt requirements_mcp.in
	$(PYTHON) -m piptools compile --upgrade -o requirements_webapi.txt requirements_webapi.in
	$(PYTHON) -m piptools compile --upgrade -o requirements-dev.txt requirements-dev.in

req-sync:
	$(PIP) install pip-tools
	$(PYTHON) -m piptools sync requirements.txt requirements_mcp.txt requirements_webapi.txt requirements-dev.txt

ai-fix:
	$(PYTHON) scripts/ai_fix.py --check-cmd "make check" --iterations 3
