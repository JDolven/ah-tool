"""Windows toast notifications via PowerShell + WinRT (stdlib only, never raises)."""
import base64
import subprocess
import sys

APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def build_script(title, body):
    """PowerShell script; text is passed as base64 (UTF-8) so no escaping issues are possible."""
    return (
        "$ErrorActionPreference='Stop'\n"
        "$title=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))\n"
        "$body=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))\n"
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
        "$xml=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n"
        "$t=$xml.GetElementsByTagName('text')\n"
        "$t.Item(0).AppendChild($xml.CreateTextNode($title)) | Out-Null\n"
        "$t.Item(1).AppendChild($xml.CreateTextNode($body)) | Out-Null\n"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($xml)\n"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s').Show($toast)\n"
    ) % (_b64(title), _b64(body), APP_ID.replace("'", "''"))


def encode_command(script):
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _argv(title, body):
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", encode_command(build_script(title, body))]


def send_toast(title, body, wait=False, timeout=20):
    """Show a toast. wait=False: fire and forget (returns {"started": True}); wait=True: returns
    {"ok", "returncode", "stderr"}. Never raises."""
    if sys.platform != "win32":
        return {"ok": False, "error": "not windows"}
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen(_argv(str(title)[:200], str(body)[:500]), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE if wait else subprocess.DEVNULL,
                             stderr=subprocess.PIPE if wait else subprocess.DEVNULL, creationflags=flags)
        if not wait:
            return {"started": True}
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            return {"ok": False, "returncode": None, "stderr": "timeout"}
        return {"ok": p.returncode == 0, "returncode": p.returncode,
                "stderr": (err or b"").decode("utf-8", "replace").strip()[:500]}
    except Exception as e:  # noqa
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
