"""Activación de controles de navegación sin esperar cuadros de animación.

Solo para consultas, cierre de formularios, salida y retorno al ingreso. No confirma citas.
"""


def navigation_click(control, timeout=2_000):
    if control.count() != 1:
        raise ValueError("El control de navegación no es único.")
    activated = control.evaluate("""node => {
        const style = getComputedStyle(node);
        if (!node.isConnected || !node.getClientRects().length || style.display === 'none'
            || style.visibility === 'hidden' || node.disabled || node.matches(':disabled')
            || node.closest('[inert], [aria-disabled="true"]') || typeof node.click !== 'function') return false;
        node.click();
        return true;
    }""", timeout=timeout)
    if not activated:
        raise ValueError("El control de navegación no está disponible.")
