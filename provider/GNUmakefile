BINARY    = terraform-provider-terrapod
HOSTNAME  = terrapod.local
NAMESPACE = default
TYPE      = terrapod
VERSION  ?= 0.0.1
OS_ARCH  ?= $(shell go env GOOS)_$(shell go env GOARCH)

default: build

# Only provider sources determine the dev version; unrelated dirty files must not change its bytes.
.PHONY: build
build:
	go build -buildvcs=false -trimpath -ldflags "-s -w -X main.version=$(VERSION)" -o $(BINARY)

.PHONY: dev-package
dev-package:
	bash ./dev-package.sh build "$(BASE_VERSION)"

.PHONY: install
install: build
	mkdir -p ~/.terraform.d/plugins/$(HOSTNAME)/$(NAMESPACE)/$(TYPE)/$(VERSION)/$(OS_ARCH)
	cp $(BINARY) ~/.terraform.d/plugins/$(HOSTNAME)/$(NAMESPACE)/$(TYPE)/$(VERSION)/$(OS_ARCH)/

.PHONY: test
test:
	go test ./... -v -count=1 -timeout 120s

.PHONY: lint
lint:
	golangci-lint run ./...

.PHONY: fmt
fmt:
	gofmt -s -w .

.PHONY: vet
vet:
	go vet ./...

.PHONY: docs
docs:
	go generate ./...

.PHONY: clean
clean:
	rm -f $(BINARY)
