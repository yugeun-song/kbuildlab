KBL ?= $(shell command -v kbuildlab)
TREES := $(shell $(KBL) trees -q)

export KBL_WORKSPACE := $(CURDIR)
export KBL_CLANGD := 1

.NOTPARALLEL:
.PHONY: all $(TREES)

all: $(TREES)

$(TREES):
	$(KBL) tags $@
