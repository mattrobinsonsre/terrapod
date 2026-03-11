BINARY    = terraform-provider-terrapod
HOSTNAME  = terrapod.local
NAMESPACE = default
TYPE      = terrapod
VERSION  ?= 0.0.1
OS_ARCH  ?= $(shell go env GOOS)_$(shell go env GOARCH)

default: build

.PHONY: build
build:
	go build -trimpath -ldflags "-s -w -X main.version=$(VERSION)" -o $(BINARY)

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
