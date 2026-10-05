"""Static route for the manual-correction UI extension."""


def register_routes(registry, core):
    def manual_feedback_js(http, _params):
        return http.static("manual_feedback.js", "application/javascript; charset=utf-8")

    registry.register(
        "GET",
        "manual_feedback.static",
        r"^/manual_feedback\.js$",
        manual_feedback_js,
        require_trusted=True,
        require_runtime=False,
        priority=220,
    )
    return registry


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

    do_get._manual_feedback_static_fallback = original_get
    core.Handler.do_GET = do_get
    core.Handler._manual_feedback_static_installed = True


def uninstall_legacy(core):
    """Remove only this module's compatibility wrapper before the final server binds."""
    current = core.Handler.do_GET
    fallback = getattr(current, "_manual_feedback_static_fallback", None)
    if fallback is None:
        return False
    core.Handler.do_GET = fallback
    core.Handler._manual_feedback_static_installed = False
    return True
