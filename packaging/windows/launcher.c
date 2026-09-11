/*
 * Windows Launcher for Fan Control
 * Launches the appropriate Python script (fan-gui.py, fan-ctl.py, fan-daemon.py)
 * finding Python in bundled portable directory, venv, PATH, or standard Windows locations.
 */

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif

#include <windows.h>
#include <shlwapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#pragma comment(lib, "shlwapi.lib")

#ifndef GUI_APP
#define GUI_APP 0
#endif

static BOOL file_exists(const wchar_t *path) {
    DWORD attr = GetFileAttributesW(path);
    return (attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY));
}

static BOOL dir_exists(const wchar_t *path) {
    DWORD attr = GetFileAttributesW(path);
    return (attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY));
}

static void show_error(const wchar_t *title, const wchar_t *message) {
#if GUI_APP
    MessageBoxW(NULL, message, title, MB_ICONERROR | MB_OK);
#else
    fwprintf(stderr, L"[%ls] %ls\n", title, message);
#endif
}

static const wchar_t *get_target_script(const wchar_t *exePath) {
    const wchar_t *fileName = PathFindFileNameW(exePath);
    wchar_t lowerName[MAX_PATH];
    wcsncpy(lowerName, fileName, MAX_PATH - 1);
    lowerName[MAX_PATH - 1] = L'\0';
    _wcslwr(lowerName);

    if (wcsstr(lowerName, L"ctl") != NULL) {
        return L"fan-ctl.py";
    } else if (wcsstr(lowerName, L"daemon") != NULL) {
        return L"fan-daemon.py";
    } else {
        return L"fan-gui.py";
    }
}

static BOOL find_python(const wchar_t *appDir, wchar_t *pythonPath, size_t maxLen, BOOL isGui, BOOL *outUsePyLauncher) {
    *outUsePyLauncher = FALSE;
    wchar_t candidate[MAX_PATH];

    // 1. Check local/bundled portable python
    if (isGui) {
        wnsprintfW(candidate, MAX_PATH, L"%ls\\python\\pythonw.exe", appDir);
        if (file_exists(candidate)) { wcsncpy(pythonPath, candidate, maxLen); return TRUE; }
    }
    wnsprintfW(candidate, MAX_PATH, L"%ls\\python\\python.exe", appDir);
    if (file_exists(candidate)) { wcsncpy(pythonPath, candidate, maxLen); return TRUE; }

    // 2. Check virtual environment in app dir (.venv or venv)
    if (isGui) {
        wnsprintfW(candidate, MAX_PATH, L"%ls\\.venv\\Scripts\\pythonw.exe", appDir);
        if (file_exists(candidate)) { wcsncpy(pythonPath, candidate, maxLen); return TRUE; }
        wnsprintfW(candidate, MAX_PATH, L"%ls\\venv\\Scripts\\pythonw.exe", appDir);
        if (file_exists(candidate)) { wcsncpy(pythonPath, candidate, maxLen); return TRUE; }
    }
    wnsprintfW(candidate, MAX_PATH, L"%ls\\.venv\\Scripts\\python.exe", appDir);
    if (file_exists(candidate)) { wcsncpy(pythonPath, candidate, maxLen); return TRUE; }
    wnsprintfW(candidate, MAX_PATH, L"%ls\\venv\\Scripts\\python.exe", appDir);
    if (file_exists(candidate)) { wcsncpy(pythonPath, candidate, maxLen); return TRUE; }

    // 3. Search in PATH
    if (isGui) {
        if (SearchPathW(NULL, L"pythonw.exe", NULL, MAX_PATH, candidate, NULL) > 0 && file_exists(candidate)) {
            wcsncpy(pythonPath, candidate, maxLen);
            return TRUE;
        }
    }
    if (SearchPathW(NULL, L"python.exe", NULL, MAX_PATH, candidate, NULL) > 0 && file_exists(candidate)) {
        wcsncpy(pythonPath, candidate, maxLen);
        return TRUE;
    }

    // 4. Check %LOCALAPPDATA%\Programs\Python\Python3*
    wchar_t localAppData[MAX_PATH];
    if (GetEnvironmentVariableW(L"LOCALAPPDATA", localAppData, MAX_PATH) > 0) {
        const wchar_t *versions[] = { L"Python314", L"Python313", L"Python312", L"Python311", L"Python310" };
        for (int i = 0; i < 5; i++) {
            if (isGui) {
                wnsprintfW(candidate, MAX_PATH, L"%ls\\Programs\\Python\\%ls\\pythonw.exe", localAppData, versions[i]);
                if (file_exists(candidate)) { wcsncpy(pythonPath, candidate, maxLen); return TRUE; }
            }
            wnsprintfW(candidate, MAX_PATH, L"%ls\\Programs\\Python\\%ls\\python.exe", localAppData, versions[i]);
            if (file_exists(candidate)) { wcsncpy(pythonPath, candidate, maxLen); return TRUE; }
        }
    }

    // 5. Check %ProgramFiles%\Python3*
    wchar_t progFiles[MAX_PATH];
    if (GetEnvironmentVariableW(L"ProgramFiles", progFiles, MAX_PATH) > 0) {
        const wchar_t *versions[] = { L"Python314", L"Python313", L"Python312", L"Python311", L"Python310" };
        for (int i = 0; i < 5; i++) {
            if (isGui) {
                wnsprintfW(candidate, MAX_PATH, L"%ls\\%ls\\pythonw.exe", progFiles, versions[i]);
                if (file_exists(candidate)) { wcsncpy(pythonPath, candidate, maxLen); return TRUE; }
            }
            wnsprintfW(candidate, MAX_PATH, L"%ls\\%ls\\python.exe", progFiles, versions[i]);
            if (file_exists(candidate)) { wcsncpy(pythonPath, candidate, maxLen); return TRUE; }
        }
    }

    // 6. Check Windows Python Launcher (py.exe)
    if (SearchPathW(NULL, L"py.exe", NULL, MAX_PATH, candidate, NULL) > 0 && file_exists(candidate)) {
        wcsncpy(pythonPath, candidate, maxLen);
        *outUsePyLauncher = TRUE;
        return TRUE;
    }

    return FALSE;
}

static const wchar_t *skip_first_arg(const wchar_t *cmdLine) {
    if (!cmdLine) return L"";
    while (*cmdLine == L' ' || *cmdLine == L'\t') cmdLine++;

    if (*cmdLine == L'"') {
        cmdLine++;
        while (*cmdLine && *cmdLine != L'"') cmdLine++;
        if (*cmdLine == L'"') cmdLine++;
    } else {
        while (*cmdLine && *cmdLine != L' ' && *cmdLine != L'\t') cmdLine++;
    }

    while (*cmdLine == L' ' || *cmdLine == L'\t') cmdLine++;
    return cmdLine;
}

static int run_launcher(void) {
    wchar_t exePath[MAX_PATH];
    if (!GetModuleFileNameW(NULL, exePath, MAX_PATH)) {
        show_error(L"Fan Control Error", L"Could not determine executable path.");
        return 1;
    }

    wchar_t appDir[MAX_PATH];
    wcsncpy(appDir, exePath, MAX_PATH - 1);
    appDir[MAX_PATH - 1] = L'\0';
    PathRemoveFileSpecW(appDir);

    const wchar_t *scriptName = get_target_script(exePath);

    // Locate target script: check appDir, then parent directory (if in bin/ or dist/)
    wchar_t scriptPath[MAX_PATH];
    wnsprintfW(scriptPath, MAX_PATH, L"%ls\\%ls", appDir, scriptName);
    if (!file_exists(scriptPath)) {
        wnsprintfW(scriptPath, MAX_PATH, L"%ls\\..\\%ls", appDir, scriptName);
        if (!file_exists(scriptPath)) {
            wchar_t errMsg[MAX_PATH * 2];
            wnsprintfW(errMsg, sizeof(errMsg)/sizeof(wchar_t),
                       L"Could not locate the script '%ls' in '%ls'.\nEnsure the application files are intact.",
                       scriptName, appDir);
            show_error(L"Fan Control Error", errMsg);
            return 1;
        }
    }

    // Find Python
    wchar_t pythonPath[MAX_PATH];
    BOOL usePyLauncher = FALSE;
    BOOL isGui = GUI_APP;
    if (!find_python(appDir, pythonPath, MAX_PATH, isGui, &usePyLauncher)) {
        show_error(L"Python 3.10+ Required",
                   L"Fan Control requires Python 3.10 or newer.\n\n"
                   L"Please install Python from https://www.python.org or the Microsoft Store,\n"
                   L"or ensure python.exe is added to your system PATH.");
        return 1;
    }

    // Construct command line
    const wchar_t *extraArgs = skip_first_arg(GetCommandLineW());
    size_t cmdLineLen = wcslen(pythonPath) + wcslen(scriptPath) + wcslen(extraArgs) + 64;
    wchar_t *fullCmdLine = (wchar_t *)malloc(cmdLineLen * sizeof(wchar_t));
    if (!fullCmdLine) {
        show_error(L"Fan Control Error", L"Out of memory allocating command line buffer.");
        return 1;
    }

    if (usePyLauncher) {
        if (extraArgs && *extraArgs) {
            wnsprintfW(fullCmdLine, cmdLineLen, L"\"%ls\" -3 \"%ls\" %ls", pythonPath, scriptPath, extraArgs);
        } else {
            wnsprintfW(fullCmdLine, cmdLineLen, L"\"%ls\" -3 \"%ls\"", pythonPath, scriptPath);
        }
    } else {
        if (extraArgs && *extraArgs) {
            wnsprintfW(fullCmdLine, cmdLineLen, L"\"%ls\" \"%ls\" %ls", pythonPath, scriptPath, extraArgs);
        } else {
            wnsprintfW(fullCmdLine, cmdLineLen, L"\"%ls\" \"%ls\"", pythonPath, scriptPath);
        }
    }

    // Set working directory to script directory
    wchar_t workingDir[MAX_PATH];
    wcsncpy(workingDir, scriptPath, MAX_PATH - 1);
    workingDir[MAX_PATH - 1] = L'\0';
    PathRemoveFileSpecW(workingDir);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    BOOL success = CreateProcessW(
        NULL,
        fullCmdLine,
        NULL,
        NULL,
        TRUE,
        0,
        NULL,
        workingDir,
        &si,
        &pi
    );

    free(fullCmdLine);

    if (!success) {
        DWORD err = GetLastError();
        wchar_t errMsg[256];
        wnsprintfW(errMsg, 256, L"Failed to start Python process.\nError code: %lu", err);
        show_error(L"Fan Control Launch Failed", errMsg);
        return (int)err;
    }

    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD exitCode = 0;
    GetExitCodeProcess(pi.hProcess, &exitCode);

    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);

    return (int)exitCode;
}

#if GUI_APP
int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPWSTR lpCmdLine, int nCmdShow) {
    (void)hInstance; (void)hPrevInstance; (void)lpCmdLine; (void)nCmdShow;
    return run_launcher();
}
#else
int wmain(int argc, wchar_t *argv[]) {
    (void)argc; (void)argv;
    return run_launcher();
}
#endif
