build-ScannerFunction: _build
build-ProcessorFunction: _build
build-WebhookFunction: _build
build-DashboardFunction: _build
build-LiveFunction: _build

_build:
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp -r src "$(ARTIFACTS_DIR)/src"
