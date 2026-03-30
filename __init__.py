# PWTT QGIS Plugin - Multi-Backend SAR Damage Detection

def classFactory(iface):
    try:
        from .plugin import PWTTPlugin
        return PWTTPlugin(iface)
    except Exception as exc:
        # Log and re-raise so QGIS shows a meaningful error instead of
        # silently dropping the plugin from the manager.
        try:
            from qgis.core import Qgis, QgsMessageLog
            QgsMessageLog.logMessage(
                f"PWTT plugin failed to load: {exc}", "PWTT", Qgis.Critical,
            )
        except Exception:
            pass
        raise
