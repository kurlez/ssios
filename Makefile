PYTHON ?= python3
export PYTHONPATH := $(CURDIR)/src

.PHONY: init up demo test preflight memfs-info

init:
	$(PYTHON) -m ssios init --state .ssios --config config/ssios.example.toml

up:
	$(PYTHON) -m ssios up --profile dev --state .ssios --config config/ssios.example.toml

demo:
	$(PYTHON) -m ssios demo --state .ssios-demo --config config/ssios.example.toml

test:
	$(PYTHON) -m unittest discover -s tests -v

preflight:
	$(PYTHON) -m ssios preflight --profile production --state .ssios-preflight --config config/ssios.example.toml

memfs-info:
	$(PYTHON) -m ssios memfs-info
