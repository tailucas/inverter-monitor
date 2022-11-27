APP := inverter_monitor
DOCKER_APP := inverter-monitor

all: help

help:
	@echo "Depends on 1Password Connect Server: https://developer.1password.com/docs/connect/get-started"

setup: docker-compose.template
	@echo "Generating docker-compose.yml"
	python3 pylib/cred_tool ENV $(APP) | python3 pylib/yaml_interpol services/app/environment docker-compose.template > docker-compose.yml

pydeps:
	python -m pip install --upgrade pip
	python -m pip install --upgrade setuptools
	python -m pip install --upgrade wheel
	python -m pip install --upgrade -r "requirements.txt"
	python -m pip install --upgrade -r "./pylib/requirements.txt"

build:
	sudo chown $(USER) data/.*
	docker-compose build

run:
	docker-compose up

connect:
	./connect_to_app.sh $(DOCKER_APP)

clean:
	rm docker-compose.yml

.PHONY: all help setup run connect clean pydeps
