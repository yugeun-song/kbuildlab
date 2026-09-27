KBL ?= $(shell command -v kbuildlab)
TREES := $(patsubst %/tree.conf,%,$(wildcard */tree.conf))

export KBL_WORKSPACE := $(CURDIR)
export KBL_CLANGD := 1

.NOTPARALLEL:
.PHONY: all $(TREES)

all: $(TREES)

$(TREES):
	$(KBL) tags $@
