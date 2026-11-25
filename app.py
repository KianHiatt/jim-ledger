import sys
import os
import traceback
import struct
import math
import subprocess

try:
    import json
    import urllib.request
    import urllib.error
    from duckduckgo_search import DDGS
    from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                                 QHBoxLayout, QTextEdit, QLineEdit, QPushButton, QLabel, 
                                 QFileDialog, QSplitter, QTabWidget, QTableWidget, 
                                 QTableWidgetItem, QHeaderView)
    from PyQt6.QtCore import Qt, QThread, pyqtSignal, QPointF, QFileSystemWatcher, QTimer
    from PyQt6.QtGui import QPainter, QPen, QColor
except Exception as e:
    print(f'[CRITICAL IMPORT ERROR]: {e}')
    traceback.print_exc()
    input('Press Enter to exit...')
    sys.exit(1)

class EngineThread(QThread):
    response_signal = pyqtSignal(str)

    def __init__(self, prompt, file_content=""):
        super().__init__()
        self.prompt = prompt
        self.file_content = file_content

    def search_web(self, query):
        try:
            with DDGS() as ddgs:
                results = [r for r in ddgs.text(query, max_results=3)]
                if results:
                    return "\n".join([f"Source: {r.get('title')} - {r.get('href')}\nContent: {r.get('body')}" for r in results])
        except Exception as e:
            return f"[Web search failed: {e}]"
        return "No relevant web results found."

    def run(self):
        web_context = ""
        lower_prompt = self.prompt.lower()
        if any(kw in lower_prompt for kw in ["latest", "standard", "price", "current", "spec", "news", "code", "regulation"]):
            search_data = self.search_web(self.prompt)
            web_context = f"\n\n[Live Web Search Context]:\n{search_data}\n"

        file_context = ""
        if self.file_content:
            file_context = f"\n\n[Active CAD/Spec Context]:\n{self.file_content}\n"

        augmented_prompt = f"{self.prompt}{file_context}{web_context}"

        url = "http://localhost:11434/api/generate"
        payload = {
            "model": "qwen2.5-coder:14b",
            "system": (
                "You are JIM (Just an Intelligent Machine), an elite engineering AI modeled after JARVIS. \n\n"
                "CRITICAL BEHAVIORAL RULES:\n"
                "1. ZERO FLUFF / ANSWER FIRST: Provide strictly the final numerical result, specification, or direct conclusion immediately.\n"
                "2. MANDATORY FORMAT: You MUST append ', sir.' to the end of every single response without exception (e.g., '80%, sir.').\n"
                "3. NEVER parrot, repeat, or recite back the user's notes or specs word-for-word.\n"
                "4. Speak with crisp, confident authority and absolute technical precision.\n"
                "5. Never output raw LaTeX block delimiters like \\[ or \\]."
            ),
            "prompt": augmented_prompt,
            "stream": False
        }
        
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                resp_text = res_data.get("response", "No response field returned.").strip()
                if not resp_text.lower().endswith("sir."):
                    resp_text += ", sir."
                self.response_signal.emit(resp_text)
        except urllib.error.URLError as e:
            self.response_signal.emit(f"[ERROR]: Could not connect to Ollama. Make sure Ollama is running. Details: {e.reason}")

class Native3DCanvas(QWidget):
    """High-performance 3D STL mesh parser and live-sync wireframe renderer"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.angle_x = 30.0
        self.angle_y = 45.0
        self.scale_factor = 1.0
        self.last_pos = None
        self.model_name = "Default Sphere Mesh (.stl)"
        self.mesh_stats = "Procedural Primitive"
        self.edges = []
        self.load_default_sphere()
        self.setMinimumSize(400, 400)

    def load_default_sphere(self):
        self.edges = []
        radius = 2.0
        for i in range(-4, 5):
            z_offset = i * 0.45
            r_ring = math.sqrt(max(0.0, radius**2 - z_offset**2))
            if r_ring == 0:
                continue
            ring_pts = []
            for j in range(24):
                angle = j * (2 * math.pi / 24)
                ring_pts.append((r_ring * math.cos(angle), r_ring * math.sin(angle), z_offset))
            for k in range(len(ring_pts)):
                self.edges.append((ring_pts[k], ring_pts[(k+1)%len(ring_pts)]))
        self.mesh_stats = "Procedural Sphere"

    def load_stl_file(self, filepath, preserve_view=False):
        new_edges = []
        min_coords = [float('inf')] * 3
        max_coords = [float('-inf')] * 3
        tri_count = 0

        try:
            with open(filepath, "rb") as f:
                f.read(80)
                num_triangles_bytes = f.read(4)
                if len(num_triangles_bytes) < 4:
                    return False
                num_triangles = struct.unpack("<I", num_triangles_bytes)[0]
                tri_count = num_triangles

                for _ in range(num_triangles):
                    data = f.read(50)
                    if len(data) < 50:
                        break
                    vals = struct.unpack("<12fH", data)
                    v1 = (vals[3], vals[4], vals[5])
                    v2 = (vals[6], vals[7], vals[8])
                    v3 = (vals[9], vals[10], vals[11])

                    for v in (v1, v2, v3):
                        for c in range(3):
                            if v[c] < min_coords[c]: min_coords[c] = v[c]
                            if v[c] > max_coords[c]: max_coords[c] = v[c]

                    new_edges.append((v1, v2))
                    new_edges.append((v2, v3))
                    new_edges.append((v3, v1))

            if new_edges:
                dims = [max_coords[i] - min_coords[i] for i in range(3)]
                center = [(min_coords[i] + max_coords[i]) / 2.0 for i in range(3)]
                max_span = max(dims)
                scale = 120.0 / (max_span if max_span > 0 else 1.0)

                normalized_edges = []
                for p1, p2 in new_edges:
                    np1 = tuple((p1[i] - center[i]) * scale for i in range(3))
                    np2 = tuple((p2[i] - center[i]) * scale for i in range(3))
                    normalized_edges.append((np1, np2))

                self.edges = normalized_edges
                self.mesh_stats = f"Triangles: {tri_count:,} | Bounds (X×Y×Z): {dims[0]:.2f} × {dims[1]:.2f} × {dims[2]:.2f} units"
                if not preserve_view:
                    self.scale_factor = 1.0
                self.update()
                return True
        except Exception as e:
            print(f"Error parsing STL: {e}")
        return False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.last_pos = event.pos()

    def mouseMoveEvent(self, event):
        if self.last_pos is not None:
            dx = event.pos().x() - self.last_pos.x()
            dy = event.pos().y() - self.last_pos.y()
            self.angle_y += dx * 0.5
            self.angle_x += dy * 0.5
            self.last_pos = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        self.last_pos = None

    def wheelEvent(self, event):
        angle = event.angleDelta().y()
        if angle > 0:
            self.scale_factor *= 1.1
        else:
            self.scale_factor *= 0.9
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#141419"))

        painter.setPen(QColor("#00FFCC"))
        painter.drawText(20, 30, f"Active Model: {self.model_name} [DUAL-SYNC ACTIVE]")
        painter.setPen(QColor("#6b7280"))
        painter.drawText(20, 50, f"{self.mesh_stats}")
        painter.drawText(20, 70, "Controls: Click & Drag to Rotate | Scroll to Zoom")

        painter.setPen(QPen(QColor("#00FFCC"), 1))
        center_x = self.width() / 2
        center_y = self.height() / 2

        rad_x = math.radians(self.angle_x)
        rad_y = math.radians(self.angle_y)

        for p1, p2 in self.edges:
            x1, y1, z1 = p1[0]*self.scale_factor, p1[1]*self.scale_factor, p1[2]*self.scale_factor
            x1_rot = x1 * math.cos(rad_y) + z1 * math.sin(rad_y)
            z1_rot = -x1 * math.sin(rad_y) + z1 * math.cos(rad_y)
            y1_rot = y1 * math.cos(rad_x) - z1_rot * math.sin(rad_x)

            x2, y2, z2 = p2[0]*self.scale_factor, p2[1]*self.scale_factor, p2[2]*self.scale_factor
            x2_rot = x2 * math.cos(rad_y) + z2 * math.sin(rad_y)
            z2_rot = -x2 * math.sin(rad_y) + z2 * math.cos(rad_y)
            y2_rot = y2 * math.cos(rad_x) - z2_rot * math.sin(rad_x)

            painter.drawLine(
                QPointF(center_x + x1_rot, center_y + y1_rot),
                QPointF(center_x + x2_rot, center_y + y2_rot)
            )

class JIMWorkstation(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("JIM — Local Native Engineering Workstation")
        self.resize(1400, 900)
        self.setStyleSheet("background-color: #0f0f12; color: #00FFCC; font-family: Consolas;")

        self.attached_file_path = ""
        self.attached_file_content = ""
        self.watched_stl_path = ""
        self.watched_blend_path = ""

        self.file_watcher = QFileSystemWatcher()
        self.file_watcher.fileChanged.connect(self.on_watched_file_changed)

        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- LEFT PANEL: Terminal & Input ---
        left_widget = QWidget()
        left_layout = QVBoxLayout()

        self.title_label = QLabel("JIM ENGINE TERMINAL")
        self.title_label.setStyleSheet("font-weight: bold; color: #00FFCC; font-size: 14px;")
        left_layout.addWidget(self.title_label)

        self.output_box = QTextEdit()
        self.output_box.setReadOnly(True)
        self.output_box.setStyleSheet("background-color: #141419; color: #00FFCC; border: 1px solid #1f2937; padding: 8px;")
        left_layout.addWidget(self.output_box)

        self.file_label = QLabel("No local file attached.")
        self.file_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        left_layout.addWidget(self.file_label)

        input_layout = QHBoxLayout()
        self.input_box = QLineEdit()
        self.input_box.setPlaceholderText("Enter command or query...")
        self.input_box.setStyleSheet("background-color: #141419; color: #fff; padding: 8px; border: 1px solid #1f2937;")
        self.input_box.returnPressed.connect(self.process_input)
        input_layout.addWidget(self.input_box)

        self.file_btn = QPushButton("LOAD FILE")
        self.file_btn.setStyleSheet("background-color: #1f2937; color: #00FFCC; font-weight: bold; padding: 8px;")
        self.file_btn.clicked.connect(self.load_local_file)
        input_layout.addWidget(self.file_btn)

        self.blender_btn = QPushButton("EDIT IN BLENDER")
        self.blender_btn.setStyleSheet("background-color: #1e293b; color: #fb923c; font-weight: bold; padding: 8px;")
        self.blender_btn.clicked.connect(self.open_in_blender)
        input_layout.addWidget(self.blender_btn)

        self.clear_file_btn = QPushButton("CLEAR")
        self.clear_file_btn.setStyleSheet("background-color: #2a1215; color: #ff5555; font-weight: bold; padding: 8px;")
        self.clear_file_btn.clicked.connect(self.clear_attached_file)
        input_layout.addWidget(self.clear_file_btn)

        left_layout.addLayout(input_layout)

        self.send_btn = QPushButton("EXECUTE")
        self.send_btn.setStyleSheet("background-color: #00FFCC; color: #000; font-weight: bold; padding: 8px;")
        self.send_btn.clicked.connect(self.process_input)
        left_layout.addWidget(self.send_btn)

        left_widget.setLayout(left_layout)
        main_splitter.addWidget(left_widget)

        # --- RIGHT PANEL: Native 3D Viewport & Data Workspace ---
        right_widget = QWidget()
        right_layout = QVBoxLayout()

        self.workspace_title = QLabel("3D HOLOGRAPHIC & DATA WORKSPACE")
        self.workspace_title.setStyleSheet("font-weight: bold; color: #00FFCC; font-size: 14px;")
        right_layout.addWidget(self.workspace_title)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: 1px solid #1f2937; background: #141419; }
            QTabBar::tab { background: #0f0f12; color: #888; padding: 8px 16px; }
            QTabBar::tab:selected { background: #1f2937; color: #00FFCC; font-weight: bold; }
        """)

        self.canvas = Native3DCanvas()
        self.tabs.addTab(self.canvas, "3D Interactive Viewport")

        self.table_tab = QWidget()
        table_layout = QVBoxLayout()
        self.data_table = QTableWidget(12, 3)
        self.data_table.setHorizontalHeaderLabels(["Parameter / Spec", "Value", "Notes"])
        self.data_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.data_table.setStyleSheet("background-color: #141419; color: #fff; gridline-color: #1f2937;")
        table_layout.addWidget(self.data_table)
        self.table_tab.setLayout(table_layout)
        self.tabs.addTab(self.table_tab, "Data Tables")

        right_layout.addWidget(self.tabs)
        right_widget.setLayout(right_layout)
        main_splitter.addWidget(right_widget)

        main_splitter.setSizes([550, 850])
        self.setCentralWidget(main_splitter)

    def setup_watchers(self, stl_path):
        if self.file_watcher.files():
            self.file_watcher.removePaths(self.file_watcher.files())
        
        self.watched_stl_path = stl_path
        base_no_ext = os.path.splitext(stl_path)[0]
        blend_path = base_no_ext + ".blend"

        paths_to_watch = [stl_path]
        if os.path.exists(blend_path):
            self.watched_blend_path = blend_path
            paths_to_watch.append(blend_path)
        
        self.file_watcher.addPaths(paths_to_watch)

    def on_watched_file_changed(self, path):
        QTimer.singleShot(300, lambda: self.handle_file_update(path))

    def handle_file_update(self, path):
        if path.lower().endswith('.stl'):
            if os.path.exists(path):
                if self.canvas.load_stl_file(path, preserve_view=True):
                    self.output_box.append(f"\n>> LIVE SYNC:\nSTL mesh update detected ({os.path.basename(path)}) — viewport refreshed.\n" + "-"*50)
        elif path.lower().endswith('.blend'):
            self.output_box.append(f"\n>> LIVE SYNC:\nBlender project file modified ({os.path.basename(path)}).\n" + "-"*50)
        
        if self.watched_stl_path:
            self.setup_watchers(self.watched_stl_path)

    def open_in_blender(self):
        if not self.watched_stl_path:
            self.output_box.append("\n>> SYSTEM:\nLoad an .stl file first before launching Blender.\n" + "-"*50)
            return

        base_no_ext = os.path.splitext(self.watched_stl_path)[0]
        blend_path = base_no_ext + ".blend"
        
        if os.path.exists(blend_path):
            os.startfile(blend_path)
            self.output_box.append(f"\n>> SYSTEM:\nActive session locked to -> {os.path.basename(blend_path)} in Blender. Both project and mesh are actively monitored.\n" + "-"*50)
        else:
            self.output_box.append(f"\n>> SYSTEM:\nNo matching {os.path.basename(blend_path)} found next to the STL.\n" + "-"*50)

    def load_local_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Open Model or File", "C:\\jim", "All Files (*.*);;3D Meshes (*.stl *.obj *.ply);;Text Files (*.txt *.py *.csv)")
        if file_path:
            self.attached_file_path = file_path
            filename = os.path.basename(file_path)
            try:
                if file_path.lower().endswith('.stl'):
                    self.canvas.model_name = filename
                    self.canvas.load_stl_file(file_path)
                    self.setup_watchers(file_path)
                    self.tabs.setCurrentIndex(0)
                    self.file_label.setText(f"Loaded & Dual-Watching: {filename}")
                    self.output_box.append(f"\n>> SYSTEM:\nLoaded STL mesh -> {filename} ({self.canvas.mesh_stats}). Dual file sync active for .blend and .stl.\n" + "-"*50)
                else:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        self.attached_file_content = f.read()
                    self.file_label.setText(f"Attached File: {filename} ({len(self.attached_file_content)} chars)")
                    
                    lines = [line.strip() for line in self.attached_file_content.splitlines() if line.strip()]
                    self.data_table.setRowCount(max(12, len(lines)))
                    for i, line in enumerate(lines):
                        parts = [p.strip() for p in line.split('|')] if '|' in line else [line, "", ""]
                        self.data_table.setItem(i, 0, QTableWidgetItem(parts[0] if len(parts) > 0 else ""))
                        self.data_table.setItem(i, 1, QTableWidgetItem(parts[1] if len(parts) > 1 else ""))
                        self.data_table.setItem(i, 2, QTableWidgetItem(parts[2] if len(parts) > 2 else ""))
                    
                    self.output_box.append(f"\n>> SYSTEM:\nLoaded text spec -> {filename}. Parsed data table.\n" + "-"*50)
                
                self.file_label.setStyleSheet("color: #00FFCC; font-size: 11px;")
            except Exception as e:
                self.file_label.setText(f"Error loading file: {e}")
                self.file_label.setStyleSheet("color: #ff5555; font-size: 11px;")

    def clear_attached_file(self):
        self.attached_file_path = ""
        self.attached_file_content = ""
        self.watched_stl_path = ""
        self.watched_blend_path = ""
        if self.file_watcher.files():
            self.file_watcher.removePaths(self.file_watcher.files())
        self.file_label.setText("No local file attached.")
        self.file_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        self.canvas.model_name = "Default Sphere Mesh (.stl)"
        self.canvas.load_default_sphere()
        self.canvas.update()
        self.output_box.append(f"\n>> SYSTEM:\nFile attachment cleared, viewport reset.\n" + "-"*50)

    def process_input(self):
        text = self.input_box.text().strip()
        if not text and not self.attached_file_content:
            return
            
        display_text = text if text else "Analyze attached file/specs."
        self.output_box.append(f"\n>> USER:\n{display_text}\n")
        self.input_box.clear()

        self.thread = EngineThread(display_text, self.attached_file_content)
        self.thread.response_signal.connect(self.display_response)
        self.thread.start()

    def display_response(self, response):
        self.output_box.append(f">> JIM:\n{response}\n" + "-"*50)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = JIMWorkstation()
    win.show()
    sys.exit(app.exec())
