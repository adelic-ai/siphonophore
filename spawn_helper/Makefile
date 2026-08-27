# Builds siphonophore-spawn (contracts/spawn_helper.md). See README.md for install steps -- the
# install step (root ownership, sudoers grant) is at least as important as the build itself and is
# NOT done by `make install` alone; read the README before deploying this anywhere.

CC       ?= cc
CFLAGS   ?= -O2 -Wall -Wextra -Wformat=2 -Wconversion -std=c11 -D_FORTIFY_SOURCE=2
LDFLAGS  ?=

BIN      := siphonophore-spawn
PREFIX   := /usr/local
LIBEXEC  := $(PREFIX)/libexec

.PHONY: all clean install

all: $(BIN)

$(BIN): siphonophore-spawn.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(BIN)

# Deliberately does NOT chmod/chown for you beyond what install(1) already does as root -- run
# this via sudo, and confirm the result matches README.md's hardening checklist (root-owned,
# not writable by the broker's own user, fixed absolute path) before trusting it.
install: $(BIN)
	install -o root -g root -m 0755 -d $(LIBEXEC)
	install -o root -g root -m 0711 $(BIN) $(LIBEXEC)/$(BIN)
	install -o root -g root -m 0755 bootstrap.py $(LIBEXEC)/siphonophore-bootstrap.py
