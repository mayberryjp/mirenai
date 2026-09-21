.PHONY: install lint typecheck test initdb docker-build docker-run

install:
	pip install .[dev]

lint:
	ruff check .

typecheck:
	mypy src

test:
	pytest -q

initdb:
	python -c "from mirenai.db import init_db; init_db()"

docker-build:
	docker build -t mirenai:dev .

docker-run:
	docker compose up
