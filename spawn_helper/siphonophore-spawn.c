/* siphonophore-spawn -- the privileged helper defined by contracts/spawn_helper.md (PINNED,
 * SH-01..SH-26). Implements exactly that contract; comments below cite the SH-NN invariant each
 * block enforces so the mapping from prose to code stays checkable by inspection.
 *
 * Deliberately minimal and dependency-free: no JSON library, no regex library, nothing beyond
 * libc and the kernel syscalls this specific job needs (memfd_create, cgroup filesystem writes,
 * setuid family). Every invariant this file does NOT enforce is out of scope by design -- most
 * importantly, it does not and cannot verify that the spawn it is asked to perform was ever
 * authorized by Gate.submit() (see SH-23's own "what this does not guarantee" section in the
 * contract, and DESIGN.md section 8). This helper enforces execution-identity consistency,
 * replay prevention, and safe privilege handling -- nothing more.
 *
 * Takes no argv (SH-08, SH-22) -- everything arrives on stdin as one framed stream (SH-09, SH-10).
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <pwd.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef MFD_CLOEXEC
#define MFD_CLOEXEC 0x0001U
#endif
#ifndef MFD_ALLOW_SEALING
#define MFD_ALLOW_SEALING 0x0002U
#endif
#ifndef F_ADD_SEALS
#define F_ADD_SEALS 1033
#endif
#ifndef F_GET_SEALS
#define F_GET_SEALS 1034
#endif
#ifndef F_SEAL_WRITE
#define F_SEAL_SEAL 0x0001
#define F_SEAL_SHRINK 0x0002
#define F_SEAL_GROW 0x0004
#define F_SEAL_WRITE 0x0008
#endif

/* ---- fixed configuration (SH-26: nothing here is broker-influenced) ---------------------- */

#define UID_MIN 60000
#define UID_MAX 65535
#define USERNAME_PREFIX "sipho-core-"
#define USERNAME_MAX_LEN 64 /* "sipho-core-" (11) + up to 32 suffix chars + slack */
#define EXECUTION_ID_MAX_LEN 64

#define CGROUP_ROOT "/sys/fs/cgroup/siphonophore-core"

#define BOOTSTRAP_PYTHON "/usr/bin/python3"
#define BOOTSTRAP_SCRIPT "/usr/local/libexec/siphonophore-bootstrap.py"

#define MAX_ENVELOPE_LEN 4096u
#define MAX_CODE_LEN (1u * 1024 * 1024)     /* 1 MiB */
#define MAX_PAYLOAD_LEN (256u * 1024)       /* 256 KiB */
#define MAX_NONCE_LEN 512u

#define SOURCE_FD 3
#define PAYLOAD_FD 4
#define NONCE_FD 5

#define OVERALL_TIMEOUT_SECONDS 20

/* Env vars copied from the helper's own (sudo env_reset-sanitized) environment into the final
 * runtime's environment -- SH-04, matching siphonophore_core/execution_uid_cgroup.py's
 * DEFAULT_CHILD_ENV_KEYS exactly, so behavior is identical whether an execution goes through this
 * helper or the existing preexec_fn path. */
static const char *const CHILD_ENV_KEYS[] = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"};
#define CHILD_ENV_KEYS_COUNT (sizeof(CHILD_ENV_KEYS) / sizeof(CHILD_ENV_KEYS[0]))

/* ---- exit codes: one per SH-NN failure category, so a test can assert on the exact invariant
 * that fired rather than just "it failed somehow" ------------------------------------------- */

enum {
    EXIT_OK = 0,
    EXIT_GENERIC = 1,
    EXIT_SH12_VERSION = 12,
    EXIT_SH13_MALFORMED = 13,
    EXIT_SH14_OVERSIZED = 14,
    EXIT_SH15_SHORT_READ = 15,
    EXIT_SH16_TRAILING = 16,
    EXIT_SH17_IDENTITY = 17,
    EXIT_SH18_FD = 18,
    EXIT_SH19_PRIVDROP = 19,
    EXIT_SH21_TIMEOUT = 21,
    EXIT_SH23_REPLAY = 23,
};

/* ---- cleanup state tracked across the whole run, so any fail() can unwind whatever was
 * already allocated (SH-21: no privileged state left outstanding on any exit path) ----------- */

static struct {
    int cgroup_created;
    char cgroup_path[PATH_MAX];
} g_cleanup = {0, {0}};

/* Async-signal-safe unsigned-int-to-decimal (no snprintf/sprintf, which glibc does not
 * guarantee safe inside a signal handler). Writes into `buf` (must hold at least 11 bytes for a
 * 32-bit pid) and returns the length. Shared by both the normal fail() cleanup path and the
 * SIGALRM handler so the two don't diverge. */
static size_t uint_to_str(unsigned int v, char *buf) {
    char tmp[10];
    int n = 0;
    if (v == 0) { buf[0] = '0'; return 1; }
    while (v > 0 && n < 10) { tmp[n++] = (char)('0' + (v % 10)); v /= 10; }
    for (int i = 0; i < n; i++) buf[i] = tmp[n - 1 - i];
    return (size_t)n;
}

/* Removing a cgroup leaf this process is still a member of always fails (cgroup v2 requires an
 * empty cgroup.procs) -- and at the moment a failure is discovered, THIS process is exactly that
 * member, since SH-23 adds it before anything that can fail afterward runs. So cleanup must first
 * move this process back to the parent (CGROUP_ROOT, which has no controllers enabled in its own
 * subtree_control here, so accepting a process alongside child cgroups is legal) before the leaf
 * can be removed out from under it. Async-signal-safe: only open/write/close/rmdir, no snprintf,
 * safe to call from on_timeout() as well as fail(). */
static void cleanup_before_exit(void) {
    if (!g_cleanup.cgroup_created) return;

    int pfd = open(CGROUP_ROOT "/cgroup.procs", O_WRONLY);
    if (pfd >= 0) {
        char pidbuf[12];
        size_t n = uint_to_str((unsigned int)getpid(), pidbuf);
        ssize_t w = write(pfd, pidbuf, n);
        (void)w; /* best-effort: if this fails, the rmdir below will simply also fail, and the
                    leaf is left for manual/operator cleanup rather than silently lost track of */
        close(pfd);
    }
    rmdir(g_cleanup.cgroup_path);
}

static void fail(int code, const char *fmt, ...) __attribute__((noreturn));
static void fail(int code, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "siphonophore-spawn: refused (exit %d): ", code);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
    cleanup_before_exit();
    _exit(code);
}

static void on_timeout(int signo) {
    (void)signo;
    /* Signal-handler-safe subset only: write(), rmdir()/close() are fine, _exit() is fine.
     * No stdio here. */
    static const char msg[] = "siphonophore-spawn: refused (exit 21): overall timeout exceeded\n";
    ssize_t written = write(STDERR_FILENO, msg, sizeof(msg) - 1);
    (void)written; /* nothing meaningful to do with a write() failure inside a signal handler */
    cleanup_before_exit();
    _exit(EXIT_SH21_TIMEOUT);
}

/* ---- bounded, exact-length reads off stdin (SH-03, SH-14, SH-15) --------------------------- */

/* Reads exactly `n` bytes into `buf`. Fails SH-15 on premature EOF, SH-18-class on unexpected I/O
 * error. Never reads more than `n` -- callers are responsible for having already capped `n`
 * against SH-14's hardcoded maximums before calling this. */
static void read_exact(int fd, void *buf, size_t n) {
    unsigned char *p = buf;
    size_t got = 0;
    while (got < n) {
        ssize_t r = read(fd, p + got, n - got);
        if (r < 0) {
            if (errno == EINTR) continue;
            fail(EXIT_SH18_FD, "read failed: %s", strerror(errno));
        }
        if (r == 0) {
            fail(EXIT_SH15_SHORT_READ, "stream closed after %zu of %zu expected bytes", got, n);
        }
        got += (size_t)r;
    }
}

/* Reads one '\n'-terminated line, capped at `cap` bytes (SH-14 applied to the envelope itself).
 * Returns the line length excluding the newline; the buffer is NUL-terminated. No newline found
 * within `cap` bytes is itself a malformed-envelope condition (SH-13). */
static size_t read_header_line(int fd, char *buf, size_t cap) {
    size_t got = 0;
    while (got < cap) {
        char c;
        ssize_t r = read(fd, &c, 1);
        if (r < 0) {
            if (errno == EINTR) continue;
            fail(EXIT_SH18_FD, "read failed reading envelope: %s", strerror(errno));
        }
        if (r == 0) {
            fail(EXIT_SH15_SHORT_READ, "stream closed mid-envelope (%zu bytes read, no newline)", got);
        }
        if (c == '\n') {
            buf[got] = '\0';
            return got;
        }
        buf[got++] = c;
    }
    fail(EXIT_SH14_OVERSIZED, "envelope exceeds %zu bytes with no newline found", cap);
}

/* Confirms the stream is exhausted where the protocol expects EOF (SH-16). */
static void expect_eof(int fd) {
    char c;
    for (;;) {
        ssize_t r = read(fd, &c, 1);
        if (r < 0) {
            if (errno == EINTR) continue;
            fail(EXIT_SH18_FD, "read failed checking for trailing EOF: %s", strerror(errno));
        }
        if (r != 0) {
            fail(EXIT_SH16_TRAILING, "trailing bytes present after declared envelope lengths");
        }
        return;
    }
}

/* ---- minimal, strict parser for the fixed SH-09 envelope shape ----------------------------- */

typedef struct {
    long version;
    long uid;
    char username[USERNAME_MAX_LEN];
    char execution_id[EXECUTION_ID_MAX_LEN];
    unsigned long code_length;
    unsigned long payload_length;
    unsigned long nonce_length;
    int have_version, have_uid, have_username, have_execution_id;
    int have_code_length, have_payload_length, have_nonce_length;
} envelope_t;

static void skip_ws(const char **p) {
    while (**p == ' ' || **p == '\t') (*p)++;
}

/* Parses a JSON string value ("...") with no escape support beyond the ones this protocol's own
 * values ever need (none -- username/execution_id are restricted to a safe charset by SH-17
 * regardless, so a bare copy up to the closing quote is sufficient and anything containing a
 * backslash or control character is rejected as malformed rather than "interpreted"). */
static int parse_json_string(const char **p, char *out, size_t out_cap) {
    if (**p != '"') return 0;
    (*p)++;
    size_t n = 0;
    while (**p != '"') {
        unsigned char c = (unsigned char)**p;
        if (c == '\0' || c == '\\' || c < 0x20) return 0; /* SH-13: no escapes, no control bytes */
        if (n + 1 >= out_cap) return 0; /* SH-13/SH-17: too long to ever be a valid value here */
        out[n++] = (char)c;
        (*p)++;
    }
    (*p)++; /* closing quote */
    out[n] = '\0';
    return 1;
}

static int parse_json_uint(const char **p, unsigned long *out) {
    const char *start = *p;
    if (!(**p >= '0' && **p <= '9')) return 0;
    errno = 0;
    char *end = NULL;
    unsigned long v = strtoul(start, &end, 10);
    if (errno != 0 || end == start) return 0;
    *p = end;
    *out = v;
    return 1;
}

static int parse_json_int(const char **p, long *out) {
    const char *start = *p;
    if (!(**p >= '0' && **p <= '9')) return 0;
    errno = 0;
    char *end = NULL;
    long v = strtol(start, &end, 10);
    if (errno != 0 || end == start) return 0;
    *p = end;
    *out = v;
    return 1;
}

/* Fixed, flat, order-independent object: {"key": value, "key": value, ...}. No nesting, no
 * arrays -- the schema this protocol needs is exactly this simple, and keeping the parser this
 * narrow is itself a security property (nothing about a generic JSON grammar is exercised here
 * that this protocol doesn't need). Unknown keys, duplicate keys, and wrong-typed values are all
 * SH-13 malformed-envelope failures. */
static int parse_envelope(const char *line, envelope_t *env) {
    memset(env, 0, sizeof(*env));
    const char *p = line;
    skip_ws(&p);
    if (*p != '{') return 0;
    p++;
    skip_ws(&p);
    if (*p == '}') { p++; skip_ws(&p); return *p == '\0'; }

    for (;;) {
        skip_ws(&p);
        char key[32];
        if (!parse_json_string(&p, key, sizeof(key))) return 0;
        skip_ws(&p);
        if (*p != ':') return 0;
        p++;
        skip_ws(&p);

        if (strcmp(key, "version") == 0) {
            if (env->have_version) return 0;
            if (!parse_json_int(&p, &env->version)) return 0;
            env->have_version = 1;
        } else if (strcmp(key, "uid") == 0) {
            if (env->have_uid) return 0;
            if (!parse_json_int(&p, &env->uid)) return 0;
            env->have_uid = 1;
        } else if (strcmp(key, "username") == 0) {
            if (env->have_username) return 0;
            if (!parse_json_string(&p, env->username, sizeof(env->username))) return 0;
            env->have_username = 1;
        } else if (strcmp(key, "execution_id") == 0) {
            if (env->have_execution_id) return 0;
            if (!parse_json_string(&p, env->execution_id, sizeof(env->execution_id))) return 0;
            env->have_execution_id = 1;
        } else if (strcmp(key, "code_length") == 0) {
            if (env->have_code_length) return 0;
            if (!parse_json_uint(&p, &env->code_length)) return 0;
            env->have_code_length = 1;
        } else if (strcmp(key, "payload_length") == 0) {
            if (env->have_payload_length) return 0;
            if (!parse_json_uint(&p, &env->payload_length)) return 0;
            env->have_payload_length = 1;
        } else if (strcmp(key, "nonce_length") == 0) {
            if (env->have_nonce_length) return 0;
            if (!parse_json_uint(&p, &env->nonce_length)) return 0;
            env->have_nonce_length = 1;
        } else {
            return 0; /* unknown key: fail closed rather than silently ignore */
        }

        skip_ws(&p);
        if (*p == ',') { p++; continue; }
        if (*p == '}') { p++; break; }
        return 0;
    }
    skip_ws(&p);
    if (*p != '\0') return 0;

    return env->have_version && env->have_uid && env->have_username && env->have_execution_id &&
           env->have_code_length && env->have_payload_length && env->have_nonce_length;
}

/* ---- identity validation (SH-02, SH-17) ----------------------------------------------------- */

static int is_safe_ident_char(char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
           c == '_' || c == '-';
}

/* Matches scripts/siphonophore-useradd's own USERNAME_RE exactly: ^sipho-core-[A-Za-z0-9_-]{1,32}$
 * -- the helper and the account-creation script must agree on what a valid name looks like, or a
 * name that passes one and not the other becomes a real gap. */
static int valid_username(const char *username) {
    const char prefix[] = USERNAME_PREFIX;
    size_t prefix_len = sizeof(prefix) - 1;
    size_t len = strlen(username);
    if (len <= prefix_len) return 0;
    if (strncmp(username, prefix, prefix_len) != 0) return 0;
    size_t suffix_len = len - prefix_len;
    if (suffix_len < 1 || suffix_len > 32) return 0;
    for (size_t i = prefix_len; i < len; i++) {
        if (!is_safe_ident_char(username[i])) return 0;
    }
    return 1;
}

/* execution_id becomes a cgroup directory component (SH-23: "{CGROUP_ROOT}/exec-{execution_id}")
 * -- this is the one thing standing between broker-supplied data and a path-traversal surface
 * (e.g. "../../etc"), so it gets the same treatment SH-11 already requires for pipe/memfd
 * ownership: never trust broker-supplied data to be safe as a path component without checking. */
static int valid_execution_id(const char *execution_id) {
    size_t len = strlen(execution_id);
    if (len < 1 || len > 63) return 0;
    for (size_t i = 0; i < len; i++) {
        if (!is_safe_ident_char(execution_id[i])) return 0;
    }
    return 1;
}

/* ---- sealed-memfd source/payload transport (SH-24, SH-25) ----------------------------------- */

/* Creates a memfd, reads exactly `len` bytes from stdin into it (already SH-14-capped by the
 * caller), seals it, INDEPENDENTLY VERIFIES the seal took effect, rewinds it, then exposes a
 * freshly reopened READ-ONLY descriptor at `target_fd` -- exactly the normative order the
 * contract requires: write -> seal -> verify -> rewind -> expose read-only -> (caller drops
 * privilege) -> exec. The original O_RDWR memfd fd is closed before this returns; nothing keeps a
 * writable reference to the content past this function. */
static void seal_and_expose(const char *name, size_t len, int target_fd) {
    int fd = memfd_create(name, MFD_CLOEXEC | MFD_ALLOW_SEALING);
    if (fd < 0) fail(EXIT_SH18_FD, "memfd_create(%s) failed: %s", name, strerror(errno));

    if (len > 0) {
        /* Bounded by the caller's own SH-14 cap before this is ever invoked -- read_exact() will
         * itself fail SH-15 on a premature close, so no separate size check needed here. */
        void *scratch = malloc(len);
        if (!scratch) fail(EXIT_GENERIC, "out of memory reading %s (%zu bytes)", name, len);
        read_exact(STDIN_FILENO, scratch, len);
        ssize_t w = write(fd, scratch, len);
        free(scratch);
        if (w < 0 || (size_t)w != len) {
            fail(EXIT_SH18_FD, "short/failed write into %s memfd: %s", name, strerror(errno));
        }
    }

    if (fcntl(fd, F_ADD_SEALS, F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW) != 0) {
        fail(EXIT_SH18_FD, "sealing %s memfd failed: %s", name, strerror(errno));
    }
    int seals = fcntl(fd, F_GET_SEALS);
    int required = F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW;
    if (seals < 0 || (seals & required) != required) {
        fail(EXIT_SH18_FD, "%s memfd seal verification failed (got 0x%x, need 0x%x)", name,
             seals < 0 ? 0 : seals, required);
    }
    if (lseek(fd, 0, SEEK_SET) != 0) {
        fail(EXIT_SH18_FD, "rewinding %s memfd failed: %s", name, strerror(errno));
    }

    char procpath[64];
    snprintf(procpath, sizeof(procpath), "/proc/self/fd/%d", fd);
    int ro_fd = open(procpath, O_RDONLY);
    if (ro_fd < 0) fail(EXIT_SH18_FD, "reopening %s memfd read-only failed: %s", name, strerror(errno));
    close(fd); /* the O_RDWR reference is gone; only the read-only one survives */

    if (dup2(ro_fd, target_fd) < 0) {
        fail(EXIT_SH18_FD, "dup2 for %s onto fd %d failed: %s", name, target_fd, strerror(errno));
    }
    if (ro_fd != target_fd) close(ro_fd);
}

/* ---- close every fd not explicitly handed to the final runtime (SH-07) ---------------------- */

static void close_unexpected_fds(const int *keep, size_t keep_count) {
    DIR *d = opendir("/proc/self/fd");
    if (!d) fail(EXIT_SH18_FD, "cannot enumerate open fds: %s", strerror(errno));
    int dirfd_num = dirfd(d);
    struct dirent *ent;
    while ((ent = readdir(d)) != NULL) {
        if (ent->d_name[0] == '.') continue;
        int fd = atoi(ent->d_name);
        if (fd == dirfd_num) continue;
        int is_kept = 0;
        for (size_t i = 0; i < keep_count; i++) {
            if (keep[i] == fd) { is_kept = 1; break; }
        }
        if (!is_kept) close(fd);
    }
    closedir(d);
}

/* ---- main -------------------------------------------------------------------------------- */

int main(void) {
    /* SH-08/SH-22: no argv is ever consulted -- this binary is invoked exact-argument-free. */

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_timeout;
    sigaction(SIGALRM, &sa, NULL);
    alarm(OVERALL_TIMEOUT_SECONDS); /* SH-21 */

    /* SH-01/SH-09/SH-13: read and parse the envelope line. */
    char header_buf[MAX_ENVELOPE_LEN + 1];
    read_header_line(STDIN_FILENO, header_buf, MAX_ENVELOPE_LEN);
    envelope_t env;
    if (!parse_envelope(header_buf, &env)) fail(EXIT_SH13_MALFORMED, "envelope did not parse");

    /* SH-12: version. */
    if (env.version != 1) fail(EXIT_SH12_VERSION, "unsupported version %ld", env.version);

    /* SH-14: hardcoded maximums, independent of what the envelope claims. */
    if (env.code_length > MAX_CODE_LEN) fail(EXIT_SH14_OVERSIZED, "code_length exceeds maximum");
    if (env.payload_length > MAX_PAYLOAD_LEN) fail(EXIT_SH14_OVERSIZED, "payload_length exceeds maximum");
    if (env.nonce_length > MAX_NONCE_LEN) fail(EXIT_SH14_OVERSIZED, "nonce_length exceeds maximum");

    /* SH-17: format checks before any lookup. */
    if (env.uid < UID_MIN || env.uid > UID_MAX) fail(EXIT_SH17_IDENTITY, "uid outside allowed range");
    if (!valid_username(env.username)) fail(EXIT_SH17_IDENTITY, "username fails naming convention");
    if (!valid_execution_id(env.execution_id)) fail(EXIT_SH17_IDENTITY, "execution_id fails format check");

    /* SH-02: cross-validate uid <-> username via a real passwd lookup -- never trust the
     * envelope's uid on its own. */
    struct passwd pwbuf;
    struct passwd *pwresult = NULL;
    char pwstrbuf[4096];
    if (getpwnam_r(env.username, &pwbuf, pwstrbuf, sizeof(pwstrbuf), &pwresult) != 0 || !pwresult) {
        fail(EXIT_SH17_IDENTITY, "no such user: %s", env.username);
    }
    if ((long)pwresult->pw_uid != env.uid) {
        fail(EXIT_SH17_IDENTITY, "uid/username mismatch: getpwnam(%s).pw_uid=%ld, envelope uid=%ld",
             env.username, (long)pwresult->pw_uid, env.uid);
    }
    uid_t target_uid = pwresult->pw_uid;
    gid_t target_gid = pwresult->pw_gid;

    /* SH-23: derive the cgroup leaf from fixed configuration + the now-validated execution_id
     * (never a broker-supplied path), refuse if it already exists (one-shot: replay/collision
     * prevention -- NOT proof this execution_id was ever Gate-authorized, see the contract's own
     * trust-boundary note), create it, add this process to it -- all while still root, before any
     * of the rest proceeds. */
    snprintf(g_cleanup.cgroup_path, sizeof(g_cleanup.cgroup_path), "%s/exec-%s", CGROUP_ROOT,
              env.execution_id);
    if (mkdir(g_cleanup.cgroup_path, 0700) != 0) {
        if (errno == EEXIST) {
            fail(EXIT_SH23_REPLAY, "execution_id already consumed: %s", env.execution_id);
        }
        fail(EXIT_GENERIC, "cgroup leaf creation failed: %s", strerror(errno));
    }
    g_cleanup.cgroup_created = 1;
    {
        char procs_path[PATH_MAX + 32]; /* g_cleanup.cgroup_path is well under PATH_MAX in
                                            practice (CGROUP_ROOT + "/exec-" + <=63 chars), but
                                            size this generously so snprintf's own static-bounds
                                            checker can prove there's no truncation, not just us */
        snprintf(procs_path, sizeof(procs_path), "%s/cgroup.procs", g_cleanup.cgroup_path);
        int cgfd = open(procs_path, O_WRONLY);
        if (cgfd < 0) fail(EXIT_GENERIC, "opening cgroup.procs failed: %s", strerror(errno));
        char pidbuf[32];
        int n = snprintf(pidbuf, sizeof(pidbuf), "%d", (int)getpid());
        if (write(cgfd, pidbuf, (size_t)n) != n) {
            fail(EXIT_GENERIC, "joining cgroup failed: %s", strerror(errno));
        }
        close(cgfd);
    }

    /* SH-03: read source/payload/nonce, each exactly as declared, each already bounded above. */
    seal_and_expose("sipho-source", env.code_length, SOURCE_FD);
    seal_and_expose("sipho-payload", env.payload_length, PAYLOAD_FD);

    int have_nonce = env.nonce_length > 0;
    if (have_nonce) {
        /* Nonce keeps pipe semantics matching identity.py's nonce_pipe()/read_nonce_from_fd() --
         * small, one-shot, no sealing story needed. The helper creates this pipe itself (SH-11's
         * principle extended past the broker->helper hop), writes the bytes it just read off
         * stdin into it, and hands the read end to the final runtime at the fixed fd. */
        void *nonce_buf = malloc(env.nonce_length);
        if (!nonce_buf) fail(EXIT_GENERIC, "out of memory reading nonce");
        read_exact(STDIN_FILENO, nonce_buf, env.nonce_length);
        int pfd[2];
        if (pipe(pfd) != 0) fail(EXIT_SH18_FD, "nonce pipe() failed: %s", strerror(errno));
        ssize_t w = write(pfd[1], nonce_buf, env.nonce_length);
        free(nonce_buf);
        close(pfd[1]);
        if (w < 0 || (size_t)w != env.nonce_length) {
            fail(EXIT_SH18_FD, "short/failed write into nonce pipe: %s", strerror(errno));
        }
        if (dup2(pfd[0], NONCE_FD) < 0) fail(EXIT_SH18_FD, "dup2 for nonce onto fd %d failed", NONCE_FD);
        if (pfd[0] != NONCE_FD) close(pfd[0]);
    }

    /* SH-16: nothing should remain on stdin past the declared lengths. */
    expect_eof(STDIN_FILENO);
    close(STDIN_FILENO);

    /* SH-04: construct the final runtime's environment from a fixed allowlist, copied from the
     * helper's own (sudo env_reset-sanitized) environment -- never anything broker-supplied. */
    char *child_envp[CHILD_ENV_KEYS_COUNT + 1];
    size_t envc = 0;
    for (size_t i = 0; i < CHILD_ENV_KEYS_COUNT; i++) {
        const char *val = getenv(CHILD_ENV_KEYS[i]);
        if (!val) continue;
        size_t need = strlen(CHILD_ENV_KEYS[i]) + 1 + strlen(val) + 1;
        char *entry = malloc(need);
        if (!entry) fail(EXIT_GENERIC, "out of memory constructing child environment");
        snprintf(entry, need, "%s=%s", CHILD_ENV_KEYS[i], val);
        child_envp[envc++] = entry;
    }
    child_envp[envc] = NULL;

    /* SH-06/SH-19: drop supplementary groups, gid, uid -- in that order -- and verify the drop
     * actually took (attempt to regain root; a working drop makes that call fail). Any failure
     * here is fatal and must not fall through to exec. */
    if (setgroups(0, NULL) != 0) fail(EXIT_SH19_PRIVDROP, "setgroups failed: %s", strerror(errno));
    if (setgid(target_gid) != 0) fail(EXIT_SH19_PRIVDROP, "setgid failed: %s", strerror(errno));
    if (setuid(target_uid) != 0) fail(EXIT_SH19_PRIVDROP, "setuid failed: %s", strerror(errno));
    if (setuid(0) == 0) {
        fail(EXIT_SH19_PRIVDROP, "privilege drop did not hold: regained root after setuid()");
    }
    if (geteuid() != target_uid || getuid() != target_uid) {
        fail(EXIT_SH19_PRIVDROP, "post-drop uid mismatch");
    }

    /* SH-07: close everything except the fixed set the final runtime is entitled to. */
    int keep[5] = {STDOUT_FILENO, STDERR_FILENO, SOURCE_FD, PAYLOAD_FD, have_nonce ? NONCE_FD : -1};
    close_unexpected_fds(keep, have_nonce ? 5 : 4);

    alarm(0); /* SH-20/21: cancel the liveness timer -- from here on this is an unprivileged exec,
                 outside this contract's own timeout responsibility. */

    /* SH-20/SH-24: exec the fixed bootstrap runtime, absolute paths only, no PATH lookup, no
     * broker-controlled argv beyond what's already fixed here. */
    char *const argv[] = {(char *)BOOTSTRAP_PYTHON, (char *)BOOTSTRAP_SCRIPT, NULL};
    execve(BOOTSTRAP_PYTHON, argv, child_envp);
    /* Only reached if execve() itself failed -- privilege is already dropped, so this is a safe,
     * unprivileged failure, not a privileged fallthrough. */
    fprintf(stderr, "siphonophore-spawn: exec of bootstrap runtime failed: %s\n", strerror(errno));
    _exit(EXIT_GENERIC);
}
