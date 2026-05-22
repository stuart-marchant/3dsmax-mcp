import json
from ..server import mcp, client
from ..helpers.paths import validate_path, PathPolicyError

@mcp.tool()
def render_scene(
    width: int = 1920,
    height: int = 1080,
    output_path: str = "",
) -> str:
    """Render the current viewport in 3ds Max."""
    try:
        output_path = validate_path(output_path, purpose="write render output")
    except PathPolicyError as exc:
        return json.dumps({"error": str(exc)})

    if client.native_available:
        payload = json.dumps({"width": width, "height": height, "output_path": output_path})
        response = client.send_command(payload, cmd_type="native:render_scene", timeout=300)
        return response.get("result", "")

    safe_path = output_path.replace("\\", "/")
    output_clause = f'outputFile:"{safe_path}"' if safe_path else ""
    save_msg = f' - saved to: {safe_path}' if safe_path else ""

    maxscript = f"""(
        local bmp = render outputWidth:{width} outputHeight:{height} {output_clause} vfb:true
        if bmp != undefined then
            "Render completed ({width}x{height}){save_msg}"
        else
            "Render returned undefined - check render settings"
    )"""
    response = client.send_command(maxscript, timeout=300.0)
    return response.get("result", "")
