/* Native startup UI: no C runtime, PowerShell or .NET needed to show it. */
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <shlobj.h>
#include "payload.h"
extern const unsigned char tools_payload[], tools_payload_end[];
static WCHAR toolsPath[MAX_PATH];
static HWND status, details;
static HANDLE logFile = INVALID_HANDLE_VALUE;
static PROCESS_INFORMATION child;
static WCHAR logPath[MAX_PATH], scriptPath[MAX_PATH], readyPath[MAX_PATH];
static WCHAR directory[MAX_PATH], temporary[MAX_PATH], powershell[MAX_PATH];
static WCHAR command[2048], message[2048], logText[32769];
static char logBytes[32769];
static DWORD started, lastLogSize;
static BOOL launched, ready, ended, smoke;

static void Log(const WCHAR *text) {
    char utf8[4096]; DWORD count;
    int length = WideCharToMultiByte(CP_UTF8, 0, text, -1, utf8, sizeof(utf8), NULL, NULL);
    if (logFile != INVALID_HANDLE_VALUE && length > 1) {
        WriteFile(logFile, utf8, (DWORD)length - 1, &count, NULL);
        WriteFile(logFile, "\r\n", 2, &count, NULL);
        FlushFileBuffers(logFile);
    }
}
static void ShowLog(void) {
    if (!logPath[0]) return;
    HANDLE file = CreateFileW(logPath, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_EXISTING, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return;
    DWORD size = GetFileSize(file, NULL), count = 0;
    if (size == lastLogSize) { CloseHandle(file); return; }
    lastLogSize = size;
    if (size > 32768) SetFilePointer(file, (LONG)(size - 32768), NULL, FILE_BEGIN);
    if (ReadFile(file, logBytes, 32768, &count, NULL)) {
        int length = MultiByteToWideChar(CP_UTF8, 0, logBytes, (int)count, logText, 32768);
        logText[length] = 0;
        SetWindowTextW(details, logText);
        SendMessageW(details, EM_SETSEL, length, length);
        SendMessageW(details, EM_SCROLLCARET, 0, 0);
    }
    CloseHandle(file);
}
static void Failure(const WCHAR *step) {
    DWORD error = GetLastError();
    wsprintfW(message, L"Could not %ls (Windows error %lu).\r\nClick Open startup log and send that file to support.", step, error);
    Log(message); SetWindowTextW(status, message); ShowLog(); ended = TRUE;
}
static void Launch(void) {
    SYSTEMTIME now; SECURITY_ATTRIBUTES security = {sizeof(security), NULL, TRUE};
    GetLocalTime(&now);
    if (SHGetFolderPathW(NULL, CSIDL_DESKTOPDIRECTORY, NULL, SHGFP_TYPE_CURRENT, directory) != S_OK || lstrlenW(directory) > MAX_PATH - 85) {
        DWORD length = GetTempPathW(MAX_PATH, directory);
        if (!length || length > MAX_PATH - 85) { Failure(L"find a short writable startup log directory"); return; }
    }
    wsprintfW(logPath, L"%ls\\SLIC-Startup-%04u%02u%02u-%02u%02u%02u-%lu.txt", directory, now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond, GetCurrentProcessId());
    logFile = CreateFileW(logPath, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (logFile == INVALID_HANDLE_VALUE) {
        DWORD length = GetTempPathW(MAX_PATH, directory);
        if (!length || length > MAX_PATH - 85) { Failure(L"find a short writable startup log directory"); return; }
        wsprintfW(logPath, L"%lsSLIC-Startup-%lu.txt", directory, GetCurrentProcessId());
        logFile = CreateFileW(logPath, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    }
    if (logFile == INVALID_HANDLE_VALUE) { Failure(L"create a startup log"); return; }
    Log(L"SLIC USB Diagnostics v1.3 - native launcher visible.");
    Log(L"This launcher uses Windows system libraries only."); Log(logPath);
    if (!GetTempPathW(MAX_PATH, temporary) || lstrlenW(temporary) > MAX_PATH - 40) { Failure(L"find the temporary directory"); return; }
    if (!GetTempFileNameW(temporary, L"SLC", 0, directory)) { Failure(L"create the embedded script file"); return; }
    wsprintfW(scriptPath, L"%ls.ps1", directory);
    if (!MoveFileW(directory, scriptPath)) { DeleteFileW(directory); Failure(L"prepare the embedded script"); return; }
    wsprintfW(readyPath, L"%ls.ready", directory);
    HANDLE file = CreateFileW(scriptPath, GENERIC_WRITE, 0, NULL, TRUNCATE_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) { Failure(L"open the embedded script"); return; }
    DWORD written;
    BOOL ok = WriteFile(file, payload, sizeof(payload), &written, NULL);
    CloseHandle(file);
    if (!ok || written != sizeof(payload)) { Failure(L"write the embedded script"); return; }
    wsprintfW(toolsPath, L"%ls.tools.zip", scriptPath);
    file = CreateFileW(toolsPath, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) { Failure(L"create the embedded tools archive"); return; }
    DWORD toolsSize = (DWORD)(tools_payload_end - tools_payload);
    ok = WriteFile(file, tools_payload, toolsSize, &written, NULL);
    CloseHandle(file);
    if (!ok || written != toolsSize) { Failure(L"write the embedded tools archive"); return; }
    if (!GetSystemDirectoryW(directory, MAX_PATH) || lstrlenW(directory) > MAX_PATH - 55) { Failure(L"locate Windows PowerShell"); return; }
    wsprintfW(powershell, L"%ls\\WindowsPowerShell\\v1.0\\powershell.exe", directory);
    wsprintfW(command, L"\"%ls\" -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Normal -File \"%ls\" -ToolArchive \"%ls\" -ReadyFile \"%ls\"", powershell, scriptPath, toolsPath, readyPath);
    if (smoke) lstrcatW(command, L" -GuiSmokeTest");
    STARTUPINFOW startup = {0};
    startup.cb = sizeof(startup); startup.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
    startup.wShowWindow = SW_SHOWNORMAL; startup.hStdOutput = logFile; startup.hStdError = logFile;
    startup.hStdInput = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &security, OPEN_EXISTING, 0, NULL);
    Log(L"Starting built-in PowerShell. Its output and errors are captured below.");
    ok = CreateProcessW(powershell, command, NULL, NULL, TRUE, CREATE_NO_WINDOW, NULL, NULL, &startup, &child);
    DWORD launchError = GetLastError();
    if (startup.hStdInput != INVALID_HANDLE_VALUE) CloseHandle(startup.hStdInput);
    if (!ok) { SetLastError(launchError); Failure(L"start Windows PowerShell"); return; }
    CloseHandle(child.hThread); launched = TRUE; started = GetTickCount();
    SetWindowTextW(status, L"Opening the USB diagnostic window...\r\nKeep this window open. Startup progress and any errors appear below."); ShowLog();
}
static LRESULT CALLBACK WindowProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_CREATE:
        status = CreateWindowW(L"STATIC", L"Starting SLIC USB Diagnostics v1.3...", WS_CHILD | WS_VISIBLE, 16, 16, 680, 70, hwnd, NULL, NULL, NULL);
        details = CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", L"", WS_CHILD | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_READONLY | ES_AUTOVSCROLL, 16, 90, 680, 220, hwnd, NULL, NULL, NULL);
        CreateWindowW(L"BUTTON", L"Open startup log", WS_CHILD | WS_VISIBLE | WS_TABSTOP, 16, 325, 160, 36, hwnd, (HMENU)1, NULL, NULL);
        CreateWindowW(L"BUTTON", L"Close launcher", WS_CHILD | WS_VISIBLE | WS_TABSTOP, 190, 325, 160, 36, hwnd, (HMENU)2, NULL, NULL);
        SetTimer(hwnd, 1, 100, NULL); return 0;
    case WM_TIMER:
        if (!launched && !ended) { Launch(); return 0; }
        if (launched && !ended) {
            if (smoke && GetTickCount() - started > 45000) { TerminateProcess(child.hProcess, 3); ExitProcess(3); }
            if (!ready && GetFileAttributesW(readyPath) != INVALID_FILE_ATTRIBUTES) {
                ready = TRUE;
                SetWindowTextW(status, L"USB diagnostic window is open. Click Start diagnostics in that window.\r\nIf you cannot see it, look for SLIC Attendance in the taskbar or use Alt+Tab.");
                Log(L"Confirmed: diagnostic GUI reported its Shown event.");
            }
            if (!ready && GetTickCount() - started > 20000) SetWindowTextW(status, L"The diagnostic window has not opened yet. Startup may be slow or blocked.\r\nSee the errors below. Click Open startup log and send the file to support.");
            if (WaitForSingleObject(child.hProcess, 0) == WAIT_OBJECT_0) {
                DWORD result; GetExitCodeProcess(child.hProcess, &result);
                wsprintfW(message, L"PowerShell exited with code %lu. GUI shown: %ls.", result, ready ? L"yes" : L"no"); Log(message);
                SetWindowTextW(status, ready && result == 0 ? L"The diagnostic window has closed. Check the Desktop for your diagnostic ZIP.\r\nIf collection failed, completed files and this startup log remain available." : L"Diagnostics could not open or ended with an error.\r\nClick Open startup log and send the file to support. Errors appear below.");
                CloseHandle(child.hProcess); child.hProcess = NULL; ended = TRUE;
                DeleteFileW(scriptPath); DeleteFileW(readyPath); DeleteFileW(toolsPath);
                if (smoke) { if (logFile != INVALID_HANDLE_VALUE) CloseHandle(logFile); ExitProcess(ready && result == 0 ? 0 : 2); }
            }
        }
        ShowLog(); return 0;
    case WM_COMMAND:
        if (LOWORD(wParam) == 1 && logPath[0]) ShellExecuteW(hwnd, L"open", logPath, NULL, NULL, SW_SHOWNORMAL);
        if (LOWORD(wParam) == 2) SendMessageW(hwnd, WM_CLOSE, 0, 0);
        return 0;
    case WM_CLOSE:
        if (launched && !ended && MessageBoxW(hwnd, L"Close the startup monitor? The USB diagnostic window will keep running.", L"SLIC Diagnostics", MB_YESNO | MB_ICONQUESTION) != IDYES) return 0;
        DestroyWindow(hwnd); return 0;
    case WM_DESTROY: PostQuitMessage(0); return 0;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}
void WinMainCRTStartup(void) {
    const WCHAR *args = GetCommandLineW(), *flag = L"--ui-smoke-test";
    for (int index = 0; args[index]; ++index) {
        int match = 0;
        while (flag[match] && args[index + match] && args[index + match] == flag[match]) ++match;
        if (!flag[match]) { smoke = TRUE; break; }
    }
    HINSTANCE instance = GetModuleHandleW(NULL);
    WNDCLASSW cls = {0}; cls.lpfnWndProc = WindowProc; cls.hInstance = instance;
    cls.lpszClassName = L"SLICNativeStartup13"; cls.hCursor = LoadCursorW(NULL, IDC_ARROW);
    cls.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    if (!RegisterClassW(&cls)) ExitProcess(1);
    HWND window = CreateWindowExW(0, cls.lpszClassName, L"SLIC USB Diagnostics v1.3 - Startup monitor", WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX, CW_USEDEFAULT, CW_USEDEFAULT, 735, 420, NULL, NULL, instance, NULL);
    if (!window) { MessageBoxW(NULL, L"Windows could not create the SLIC startup window.", L"SLIC Diagnostics", MB_OK | MB_ICONERROR); ExitProcess(1); }
    ShowWindow(window, SW_SHOWNORMAL); UpdateWindow(window);
    MSG msg;
    while (GetMessageW(&msg, NULL, 0, 0) > 0) { TranslateMessage(&msg); DispatchMessageW(&msg); }
    if (logFile != INVALID_HANDLE_VALUE) CloseHandle(logFile);
    if (!launched || ended) { if (scriptPath[0]) DeleteFileW(scriptPath); if (readyPath[0]) DeleteFileW(readyPath); if (toolsPath[0]) DeleteFileW(toolsPath); }
    ExitProcess(0);
}
