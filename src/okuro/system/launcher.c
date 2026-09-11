// Okuro.app/Contents/MacOS/okuro — compiled launcher.
//
// Purpose: defeat macOS framework Python's __PYVENV_LAUNCHER__ re-exec.
//
// Background: every macOS Python (Apple's, Homebrew's) ships as a framework
// (Python.framework). When the venv's bin/python is launched, framework
// Python re-execs to its own binary at
//
//     <framework>/Versions/X.Y/Resources/Python.app/Contents/MacOS/Python
//
// after setting __PYVENV_LAUNCHER__ to remember where it came from. The
// kernel-level _NSGetExecutablePath then reports the framework path (not
// the venv path), so AppKit's NSBundle.mainBundle() walks up to Python.app
// and the Dock displays "Python" with the rocket icon — even when our
// renamed Python copy lives inside Okuro.app/Contents/MacOS/.
//
// A compiled launcher that EMBEDS Python via Py_Main escapes the dance:
// dyld loads libpython.dylib into THIS process at startup, no re-exec
// happens, and _NSGetExecutablePath returns our binary's path. AppKit
// then walks up MacOS/ → Contents/ → Okuro.app and finds the right
// Info.plist — Dock title "Okuro", icon okuro.icns, cmd-tab "Okuro".
//
// Build: see okuro.system.interpreter._try_compile_native_launcher.
// The compile is done at install time so we can pick up the user's
// actual Python.framework via ``python3-config``.

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

int main(int argc, char *argv[]) {
    // Inject "-m okuro.cli.main" between argv[0] and the user's args, then
    // hand the whole thing to Py_Main. This is the canonical PEP 405 entry
    // for an embedded interpreter that wants to run a module.
    const int new_argc = argc + 2;
    wchar_t **new_argv = (wchar_t **)calloc((size_t)new_argc + 1, sizeof(wchar_t *));
    if (new_argv == NULL) {
        fprintf(stderr, "okuro: out of memory allocating argv\n");
        return 1;
    }

    new_argv[0] = Py_DecodeLocale(argv[0], NULL);
    new_argv[1] = Py_DecodeLocale("-m", NULL);
    new_argv[2] = Py_DecodeLocale("okuro.cli.main", NULL);
    if (new_argv[0] == NULL || new_argv[1] == NULL || new_argv[2] == NULL) {
        fprintf(stderr, "okuro: failed to decode launcher argv\n");
        return 1;
    }

    for (int i = 1; i < argc; i++) {
        new_argv[i + 2] = Py_DecodeLocale(argv[i], NULL);
        if (new_argv[i + 2] == NULL) {
            fprintf(stderr, "okuro: failed to decode argv[%d]\n", i);
            return 1;
        }
    }

    int rc = Py_Main(new_argc, new_argv);

    for (int i = 0; i < new_argc; i++) {
        if (new_argv[i] != NULL) {
            PyMem_RawFree(new_argv[i]);
        }
    }
    free(new_argv);
    return rc;
}
