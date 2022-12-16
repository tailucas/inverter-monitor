APP := inverter_monitor
DOCKER_APP := inverter-monitor
USER_ID := 999
GROUP_ID := $(shell getent group docker | cut -f3 -d ':')

all: help

help:
	@echo "Depends on 1Password Connect Server: https://developer.1password.com/docs/connect/get-started"

user:
	id $(USER_ID) || sudo useradd -r -u $(USER_ID) -g $(GROUP_ID) app && sudo usermod -a -G $(GROUP_ID) -u $(USER_ID) app
	mkdir -p ./data/
	sudo chown $(USER_ID):$(GROUP_ID) ./data/
	sudo chmod 755 ./data/
	sudo chmod g+s ./data/

setup: docker-compose.template
	@echo "Generating docker-compose.yml"
	cat docker-compose.template | sed "s~__DOCKER_HOSTNAME__~$(DOCKER_APP)~g" > docker-compose.template2
	python3 ../pylib/cred_tool ENV.$(APP) $(APP) | python3 ../pylib/yaml_interpol services/app/environment docker-compose.template2 > docker-compose.yml
	rm -f docker-compose.template2

pydeps:
	python -m pip install --upgrade pip
	python -m pip install --upgrade setuptools
	python -m pip install --upgrade wheel
	python -m pip install --upgrade -r "requirements.txt"
	python -m pip install --upgrade -r "../pylib/requirements.txt"

build:
	docker-compose build

run:
	docker-compose up

connect:
	./connect_to_app.sh $(DOCKER_APP)

clean:
	rm docker-compose.yml

.PHONY: all help setup run connect clean pydeps
