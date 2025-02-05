import bpy, struct, threading, urllib.parse, os
from mathutils import Vector
from http.server import BaseHTTPRequestHandler
import bmesh
import socketserver

# --- MeshX バイナリ生成用関数群 ---

def encode_sbe(value):
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            result.append(byte | 0x80)
        else:
            result.append(byte)
            break
    return bytes(result)

def write_enumstr(s):
    b = s.encode("ascii")
    return struct.pack("<B", len(b)) + b

def export_meshx_bytes(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    # 一時メッシュを生成
    mesh = obj.evaluated_get(depsgraph).to_mesh()
    
    # --- ここで bmesh を用いて三角面化する ---
    bm = bmesh.new()
    bm.from_mesh(mesh)
    # bm.faces[:] で全ての面を対象に、三角面化（n-gon, 四角形などを三角形に分割）
    bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method='BEAUTY', ngon_method='BEAUTY')
    bm.to_mesh(mesh)
    bm.free()
    # -------------------------------------------
    
    # calc_loop_triangles() で三角形情報を算出（念のため）
    mesh.calc_loop_triangles()
    if len(mesh.uv_layers) > 0:
        mesh.calc_tangents()
    
    vertices = [v.co.copy() for v in mesh.vertices]
    vertex_count = len(vertices)
    normals = [v.normal.copy() for v in mesh.vertices]
    has_normals = True

    if len(mesh.uv_layers) > 0 and hasattr(mesh, "loops"):
        vertex_tangents = [Vector((0.0, 0.0, 0.0)) for _ in range(vertex_count)]
        vertex_tangent_w = [0.0] * vertex_count
        counts = [0] * vertex_count
        for loop in mesh.loops:
            vi = loop.vertex_index
            tangent = Vector(loop.tangent)
            w = loop.bitangent_sign
            vertex_tangents[vi] += tangent
            vertex_tangent_w[vi] += w
            counts[vi] += 1
        tangents = []
        for i in range(vertex_count):
            if counts[i]:
                avg_tan = vertex_tangents[i] / counts[i]
                avg_w = vertex_tangent_w[i] / counts[i]
            else:
                avg_tan = Vector((0.0, 0.0, 0.0))
                avg_w = 1.0
            tangents.append((avg_tan, avg_w))
        has_tangents = True
    else:
        tangents = None
        has_tangents = False

    if len(mesh.vertex_colors) > 0:
        vcol_layer = mesh.vertex_colors.active
        vertex_colors = [Vector((0.0, 0.0, 0.0, 0.0)) for _ in range(vertex_count)]
        counts = [0] * vertex_count
        for poly in mesh.polygons:
            for li in poly.loop_indices:
                vi = mesh.loops[li].vertex_index
                col = vcol_layer.data[li].color
                if len(col) == 3:
                    col = (col[0], col[1], col[2], 1.0)
                vertex_colors[vi] += Vector(col)
                counts[vi] += 1
        for i in range(vertex_count):
            if counts[i]:
                vertex_colors[i] /= counts[i]
        has_colors = True
    else:
        vertex_colors = None
        has_colors = False

    uv_channels = []
    for uv_layer in mesh.uv_layers:
        uvs = [Vector((0.0, 0.0)) for _ in range(vertex_count)]
        counts = [0] * vertex_count
        for poly in mesh.polygons:
            for li in poly.loop_indices:
                vi = mesh.loops[li].vertex_index
                uv = uv_layer.data[li].uv
                uvs[vi] += Vector((uv[0], uv[1]))
                counts[vi] += 1
        for i in range(vertex_count):
            if counts[i]:
                uvs[i] /= counts[i]
        uv_channels.append(uvs)
    uv_channel_count = len(uv_channels)

    triangles = mesh.loop_triangles
    triangle_count = len(triangles)

    out = bytearray()
    out.extend(b'\x05MeshX')
    out.extend(struct.pack("<i", 7))
    flags = 0
    if has_normals:
        flags |= 1 << 0
    if has_tangents:
        flags |= 1 << 1
    if has_colors:
        flags |= 1 << 2
    out.extend(struct.pack("<I", flags))
    out.extend(encode_sbe(vertex_count))
    out.extend(encode_sbe(1))   # submeshCount
    out.extend(encode_sbe(0))   # boneCount
    out.extend(encode_sbe(0))   # blendShapeCount
    out.extend(encode_sbe(uv_channel_count))
    for _ in range(uv_channel_count):
        out.extend(struct.pack("B", 2))
    out.extend(write_enumstr("Linear"))
    out.extend(struct.pack("B", 0))
    for v in vertices:
        out.extend(struct.pack("<3f", v.x, v.y, v.z))
    if has_normals:
        for n in normals:
            out.extend(struct.pack("<3f", n.x, n.y, n.z))
    if has_tangents:
        for t, w in tangents:
            out.extend(struct.pack("<4f", t.x, t.y, t.z, w))
    if has_colors:
        for c in vertex_colors:
            out.extend(struct.pack("<4f", c[0], c[1], c[2], c[3]))
    for channel in uv_channels:
        for uv in channel:
            out.extend(struct.pack("<2f", uv.x, uv.y))
    out.extend(write_enumstr("Triangles"))
    out.extend(encode_sbe(triangle_count))
    for tri in triangles:
        out.extend(struct.pack("<3i", tri.vertices[0], tri.vertices[1], tri.vertices[2]))
    
    # 一時的に生成したメッシュデータを解放
    obj.to_mesh_clear()
    return bytes(out)

# --- メインスレッドで Blender 操作を実行するためのコールバック ---
def create_text_and_export_main(text_value, font_value, event, result_container):
    bpy.ops.object.select_all(action='DESELECT')
    bpy.ops.object.text_add()
    text_obj = bpy.context.active_object
    text_obj.data.body = text_value
    text_obj.data.extrude = 0.1

    # font パラメータが指定されていれば、ワーキングディレクトリ内のフォントファイルを読み込む
    if font_value:
        working_dir = bpy.path.abspath("/fonts")
        font_path = os.path.join(working_dir, font_value)
        try:
            font = bpy.data.fonts.load(font_path)
            text_obj.data.font = font
            print("Loaded font:", font_path)
        except Exception as e:
            print("Failed to load font from", font_path, ":", e)

    bpy.ops.object.convert(target='MESH')
    mesh_obj = bpy.context.active_object

    binary_data = export_meshx_bytes(mesh_obj)
    result_container['data'] = binary_data

    bpy.data.objects.remove(mesh_obj, do_unlink=True)

    event.set()
    return None  # bpy.app.timers.register() の解除用

def process_request_in_main_thread(text_value, font_value):
    print("Processing request in main thread...")
    event = threading.Event()
    result_container = {}
    print("Registering timer...")
    bpy.app.timers.register(lambda: create_text_and_export_main(text_value, font_value, event, result_container))
    print("Waiting for event...")
    event.wait()
    print("Event received.")
    return result_container['data']

# --- HTTP リクエストを処理するハンドラ ---
class MeshXHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_path.query)
        text_param = query.get("text", [""])[0]
        font_param = query.get("font", [""])[0]
        self.log_message("Received text: %s, font: %s", text_param, font_param)

        binary_data = process_request_in_main_thread(text_param, font_param)

        print("Sending response...")

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(binary_data)))
        self.end_headers()
        self.wfile.write(binary_data)

    def log_message(self, format, *args):
        print(format % args)

# --- マルチスレッド対応の TCP サーバ ---
class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass

httpd = None
server_thread = None

def start_http_server(port=8000):
    global httpd, server_thread
    handler = MeshXHTTPRequestHandler
    httpd = ThreadedTCPServer(("", port), handler)
    # httpd.serve_forever()
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    print("HTTP Server started on port", port)

    def keep_alive():
        return 1.0  # 1秒ごとに空のコールバックを実行
    
    bpy.app.timers.register(keep_alive)


def stop_http_server():
    global httpd, server_thread
    if httpd:
        print("Shutting down HTTP Server...")
        httpd.shutdown()
        httpd.server_close()
        httpd = None
        server_thread = None
        print("HTTP Server stopped.")

# --- サーバー起動 ---
start_http_server(8033)