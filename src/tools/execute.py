import json
import re

from ..server import mcp, client
from ..helpers.safe_mode import check_script, SafeModeViolation


_MAXSCRIPT_ERROR_SENTINEL = "__MCP_MS_ERR__:"

# Keyword patterns → dedicated tools that handle the same intent. Each pattern
# is scanned against the user-supplied MAXScript; matches contribute their tool
# names to a deduplicated suggestion list returned in the error envelope's hint.
_SUGGESTION_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    # OSL-related failures are particularly opaque and rarely tractable by
    # retrying MAXScript. introspect_osl is the only way to learn the shader's
    # actual parameter names, output channels, and connection rules.
    (re.compile(r"\bOSLMap\b|\bOSLPath\b|\bOSLAutoUpdate\b|\bUberBitmap2?\b|\.osl\b|\bsetOSL\w*\b|\bgetOSL\w*\b", re.IGNORECASE),
     ("introspect_osl", "write_osl_shader")),
    (re.compile(r"\b(Box|Sphere|Cylinder|Cone|Plane|Pyramid|Torus|GeoSphere|Teapot|Tube)\b\s*(?:\(|\w+:)", re.IGNORECASE),
     ("create_object",)),
    (re.compile(r"\bclassOf\s+\$|\bshowProperties\b|\bgetProperty\b", re.IGNORECASE),
     ("inspect_object", "get_object_properties")),
    (re.compile(r"\baddModifier\b", re.IGNORECASE),
     ("add_modifier",)),
    (re.compile(r"\bdeleteModifier\b", re.IGNORECASE),
     ("remove_modifier",)),
    (re.compile(r"\.material\s*="),
     ("assign_material",)),
    (re.compile(r"\bmeditMaterials\b|\bgetClassName\b.*[Mm]aterial"),
     ("get_materials", "get_material_slots")),
    (re.compile(r"\.position\s*=|\.rotation\s*=|\.scale\s*=|\bmove\s+\$|\brotate\s+\$|\bscale\s+\$"),
     ("transform_object",)),
    (re.compile(r"\bselect\s+\$|\bselectmore\s+\$|\bclearSelection\b", re.IGNORECASE),
     ("select_objects",)),
    (re.compile(r"\.parent\s*="),
     ("set_parent",)),
    (re.compile(r"\.isHidden\s*=|hide\s+\$|unhide\s+\$", re.IGNORECASE),
     ("set_visibility",)),
    (re.compile(r"\.name\s*=\s*\""),
     ("batch_rename_objects",)),
    (re.compile(r"\bcopy\s+\$|\binstance\s+\$|\breference\s+\$", re.IGNORECASE),
     ("clone_objects",)),
    (re.compile(r"\bdelete\s+\$|\bdelete\s+\(getNodeByName", re.IGNORECASE),
     ("delete_objects",)),
    (re.compile(r"\bILayerManager\b|\bLayerManager\.", re.IGNORECASE),
     ("manage_layers",)),
    (re.compile(r"\bgroup\s+selection\b|\bungroup\b|\bopenGroup\b|\bcloseGroup\b", re.IGNORECASE),
     ("manage_groups",)),
    (re.compile(r"\brender\s*\(|\bmax\s+quick\s+render\b", re.IGNORECASE),
     ("render_scene",)),
    (re.compile(r"\bholdMaxFile\b|\bfetchMaxFile\b|\bresetMaxFile\b|\bsaveMaxFile\b", re.IGNORECASE),
     ("manage_scene",)),
    (re.compile(r"\bSelectionSets\b|\bnamedSelectionSets\b", re.IGNORECASE),
     ("manage_selection_sets",)),
    (re.compile(r"\bData_Channel\b|\bDataChannelModifier\b", re.IGNORECASE),
     ("add_data_channel", "add_dc_script_operator")),
    (re.compile(r"\bgetSubAnim\b|\bsetController\b|\bassignNewController\b", re.IGNORECASE),
     ("assign_controller", "inspect_controller")),
    (re.compile(r"\bparamWire\.connect\b|\bparamwire\.disconnect\b", re.IGNORECASE),
     ("wire_params", "unwire_params")),
)


def _suggest_tools(script: str) -> list[str]:
    suggestions: list[str] = []
    for pattern, tools in _SUGGESTION_RULES:
        if pattern.search(script):
            for tool in tools:
                if tool not in suggestions:
                    suggestions.append(tool)
    return suggestions


@mcp.tool()
def execute_maxscript(code: str = "", command: str = "") -> str:
    """Execute arbitrary MAXScript code in 3ds Max and return the result."""
    script = code or command
    if not script:
        return "Error: provide MAXScript code in the 'code' parameter"
    try:
        check_script(script)
    except SafeModeViolation as exc:
        return json.dumps({
            "status": "error",
            "error_type": "SafeModeViolation",
            "error": str(exc),
            "reason": exc.reason,
        })
    response = client.send_command(script, cmd_type="maxscript")
    result = response.get("result", "")

    if isinstance(result, str) and result.startswith(_MAXSCRIPT_ERROR_SENTINEL):
        message = result[len(_MAXSCRIPT_ERROR_SENTINEL):].strip()
        payload: dict[str, object] = {
            "status": "error",
            "error_type": "MAXScriptError",
            "error": message,
        }
        suggested = _suggest_tools(script)
        if suggested:
            payload["hint"] = {
                "message": (
                    "execute_maxscript is a fallback. Before retrying the same "
                    "script, consider using a dedicated MCP tool that handles "
                    "this intent directly."
                ),
                "suggested_tools": suggested,
            }
        return json.dumps(payload)

    return result
