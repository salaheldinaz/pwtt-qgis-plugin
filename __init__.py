# PWTT QGIS Plugin - Multi-Backend SAR Damage Detection

def classFactory(iface):
    from .plugin import PWTTPlugin
    return PWTTPlugin(iface)
