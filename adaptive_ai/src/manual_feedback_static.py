"""Static route for the manual-correction UI extension."""


def install(core):
    if getattr(core.Handler, "_manual_feedback_static_installed", False):
        return
    original_get = core.Handler.do_GET

    def do_get(self):
        path, _, _ = self.path.partition("?")
        if path == "/manual_feedback.js":
            if not self.require_trusted_client():
                return
            return self.static("manual_feedback.js", "application/javascript; charset=utf-8")
        return original_get(self)

    core.Handler.do_GET = do_get
    core.Handler._manual_feedback_static_installed = True
