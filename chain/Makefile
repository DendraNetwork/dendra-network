BRANCH := $(shell git rev-parse --abbrev-ref HEAD)
COMMIT := $(shell git log -1 --format='%H')
APPNAME := dendra

# do not override user values
ifeq (,$(VERSION))
  VERSION := $(shell git describe --exact-match 2>/dev/null)
  # if VERSION is empty, then populate it with branch name and raw commit hash
  ifeq (,$(VERSION))
    VERSION := $(BRANCH)-$(COMMIT)
  endif
endif

# Update the ldflags with the app, client & server names
ldflags = -X github.com/cosmos/cosmos-sdk/version.Name=$(APPNAME) \
	-X github.com/cosmos/cosmos-sdk/version.AppName=$(APPNAME)d \
	-X github.com/cosmos/cosmos-sdk/version.Version=$(VERSION) \
	-X github.com/cosmos/cosmos-sdk/version.Commit=$(COMMIT)

BUILD_FLAGS := -ldflags '$(ldflags)'

##############
###  Test  ###
##############

test-unit:
	@echo Running unit tests...
	@go test -mod=readonly -v -timeout 30m ./...

test-race:
	@echo Running unit tests with race condition reporting...
	@go test -mod=readonly -v -race -timeout 30m ./...

test-cover:
	@echo Running unit tests and creating coverage report...
	@go test -mod=readonly -v -timeout 30m -coverprofile=$(COVER_FILE) -covermode=atomic ./...
	@go tool cover -html=$(COVER_FILE) -o $(COVER_HTML_FILE)
	@rm $(COVER_FILE)

bench:
	@echo Running unit tests with benchmarking...
	@go test -mod=readonly -v -timeout 30m -bench=. ./...

test: govet govulncheck test-unit

.PHONY: test test-unit test-race test-cover bench

#################
###  Install  ###
#################

all: install

install:
	@echo "--> ensure dependencies have not been modified"
	@go mod verify
	@echo "--> installing $(APPNAME)d"
	@go install $(BUILD_FLAGS) -mod=readonly ./cmd/$(APPNAME)d

.PHONY: all install

##################
###  Protobuf  ###
##################

# Generation goes through `buf`, which is pinned in the `tool` block of go.mod: `go tool` builds it
# from the module graph, so regenerating protobuf code needs a Go toolchain and nothing else. The
# previous target shelled out to `ignite generate proto-go`, which obliged every contributor to
# install a full project scaffolder to touch a single `.pb.go`, and pinned no version of anything.
#
# `buf.gen.gogo.yaml` sets `out: .`, so buf writes its output under `proto/<module path>/...` instead
# of in place. The files are moved onto the tree they belong to and the staging tree is removed. The
# module path is read from go.mod rather than written here, so a rename of the module does not leave
# a stale path behind in this file.
GO_MODULE   := $(shell go list -m)
PROTO_STAGE := proto/$(GO_MODULE)

proto-deps:
	@echo "--> Proto plugins are declared in the tool block of go.mod; run 'go tool' to list them"

proto-gen:
	@echo "--> Generating protobuf files (buf)"
	@rm -rf "$(PROTO_STAGE)"
	@cd proto && go tool github.com/bufbuild/buf/cmd/buf generate --template buf.gen.gogo.yaml
	@test -d "$(PROTO_STAGE)" || { echo "    buf produced nothing under $(PROTO_STAGE)"; exit 1; }
	@cp -R "$(PROTO_STAGE)/." .
	@rm -rf "$(PROTO_STAGE)"
	@find proto -type d -empty -delete
	@echo "--> Generated protobuf files moved into the module tree"

# Proof that the committed generated code is exactly what the pinned toolchain produces. Regenerates
# into a staging tree and compares byte for byte, leaving the working tree untouched. A drift between
# the `.proto` files and the committed `.pb.go` is invisible to every test in the suite: the tests
# compile against the committed code, so they keep passing while the schema says something else.
#
# The `count` guard is not decoration. If generation silently produces nothing -- a broken template, a
# moved directory, a plugin that fails without a non-zero exit -- the comparison loop iterates zero
# times and every check "passes". A target that reports success after verifying nothing is worse than
# no target at all, because it is believed.
proto-check:
	@echo "--> Verifying generated protobuf files match the .proto sources"
	@rm -rf "$(PROTO_STAGE)"
	@cd proto && go tool github.com/bufbuild/buf/cmd/buf generate --template buf.gen.gogo.yaml
	@status=0; count=0; \
	for f in $$(find "$(PROTO_STAGE)" -type f -name '*.go' 2>/dev/null | sort); do \
		rel=$${f#$(PROTO_STAGE)/}; count=$$((count+1)); \
		if [ ! -f "$$rel" ]; then echo "    absent from the tree: $$rel"; status=1; \
		elif ! cmp -s "$$f" "$$rel"; then echo "    differs: $$rel"; status=1; fi; \
	done; \
	rm -rf "$(PROTO_STAGE)"; find proto -type d -empty -delete; \
	if [ "$$count" -eq 0 ]; then echo "    generation produced no file -- the target verified nothing"; exit 1; fi; \
	if [ "$$status" -ne 0 ]; then echo "    FAIL: run 'make proto-gen' and commit the result"; exit 1; fi; \
	echo "    OK: $$count generated files match the committed tree"

.PHONY: proto-deps proto-gen proto-check

#################
###  Linting  ###
#################

lint:
	@echo "--> Running linter"
	@go tool github.com/golangci/golangci-lint/cmd/golangci-lint run ./... --timeout 15m

lint-fix:
	@echo "--> Running linter and fixing issues"
	@go tool github.com/golangci/golangci-lint/cmd/golangci-lint run ./... --fix --timeout 15m

.PHONY: lint lint-fix

###################
### Development ###
###################

govet:
	@echo Running go vet...
	@go vet ./...

# The scanner is run through `go run` at a pinned version rather than being added to the `tool` block
# of go.mod. Declaring it as a module tool drags golang.org/x/vuln into the module graph, which pulls
# ten golang.org/x/* modules of the chain build forward with it (crypto, net, text, sys, ...): a
# vulnerability scanner must not be able to change what the chain is compiled against. `go run` at an
# explicit version keeps the scan reproducible and the dependency graph unchanged.
#
# Both previous lines were dead. `go tool <path>@latest` is not valid syntax -- `go tool` resolves
# names from the tool block and rejects a version suffix -- and the following line invoked a bare
# `govulncheck` binary that no step had put on PATH. `make test` therefore stopped here on a clean
# clone, before running a single test.
GOVULNCHECK_VERSION ?= v1.6.0

govulncheck:
	@echo "--> Running govulncheck ($(GOVULNCHECK_VERSION))"
	@go run golang.org/x/vuln/cmd/govulncheck@$(GOVULNCHECK_VERSION) ./...

.PHONY: govet govulncheck