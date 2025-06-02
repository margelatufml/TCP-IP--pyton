import socket
import json
import os
import threading
import time
import subprocess
from pathlib import Path
import errno
import sys


class ApplicationClient:
    def __init__(self, host='localhost', port=5000):
        self.host = host
        self.port = port
        self.downloaded_apps = {}
        self.running_apps = {}
        self.lock = threading.Lock()
        self.socket_lock = threading.RLock()
        self.socket = None
        self.notification_thread = None
        self.stop_event = threading.Event()
        self.client_id = f"{os.getpid()}-{threading.get_ident()}"
        self.downloads_dir = 'downloads'

        if not os.path.exists(self.downloads_dir):
            os.makedirs(self.downloads_dir)

    def connect(self):
        self.socket_lock.acquire()
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.host, self.port))
            print(f"Client {self.client_id}: Conectat la serverul {self.host}:{self.port}")
            self.stop_event.clear()
            self.notification_thread = threading.Thread(target=self.listen_for_notifications, name=f"NotificationListener-{self.client_id}")
            self.notification_thread.daemon = True
            self.notification_thread.start()
        finally:
            self.socket_lock.release()

    def receive_json(self):
        buffer = b''
        current_socket_timeout = None
        try:
            if not self.socket or self.socket.fileno() == -1:
                print(f"Client {self.client_id}: receive_json: Socket not valid for receive_json")
                return {"status": "error", "message": "Socket not valid for receive_json"}

            current_socket_timeout = self.socket.gettimeout()
            self.socket.settimeout(15.0)

            start_time = time.time()
            while True:
                if time.time() - start_time > 15.0:
                    raise socket.timeout("Timeout total (15s) depășit pentru primirea JSON complet.")

                chunk = self.socket.recv(4096)
                if not chunk:
                    if buffer:
                        raise json.JSONDecodeError("Conexiune închisă, buffer JSON incomplet.",
                                                   buffer.decode('utf-8', errors='replace'), 0)
                    if current_socket_timeout is not None: self.socket.settimeout(current_socket_timeout) # Restore
                    return None

                buffer += chunk
                try:
                    json_obj = json.loads(buffer.decode('utf-8'))
                    if current_socket_timeout is not None: self.socket.settimeout(current_socket_timeout) # Restore
                    return json_obj
                except UnicodeDecodeError as ude:
                    print(f"Client {self.client_id}: Avertisment (receive_json): Eroare de decodare Unicode în buffer: {ude}. Buffer: {buffer!r}")
                except json.JSONDecodeError:
                    if len(buffer) > 2 * 1024 * 1024:
                        if current_socket_timeout is not None: self.socket.settimeout(current_socket_timeout)
                        raise json.JSONDecodeError("Buffer JSON prea mare (peste 2MB), posibil eroare protocol.", buffer.decode('utf-8','replace'),0)
                    pass

        except socket.timeout as e_timeout:
            print(f"Client {self.client_id} (receive_json): Timeout. {e_timeout}")
            if current_socket_timeout is not None and self.socket and self.socket.fileno() != -1: self.socket.settimeout(current_socket_timeout)
            return {"status": "error", "message": f"Timeout la citirea JSON: {e_timeout}"}
        except Exception as e:
            print(f"Client {self.client_id} (receive_json): Eroare critică: {e}. Buffer: {buffer[:100]}...")
            if current_socket_timeout is not None and self.socket and self.socket.fileno() != -1: self.socket.settimeout(current_socket_timeout)
            return {"status": "error", "message": f"Eroare la citirea JSON: {e}"}
        if current_socket_timeout is not None and self.socket and self.socket.fileno() != -1: self.socket.settimeout(current_socket_timeout)
        return None

    def get_applications_list(self):
        request = {'command': 'list_apps'}
        self.socket_lock.acquire()
        try:
            if not self.socket or (hasattr(self.socket, '_closed') and self.socket._closed) or self.socket.fileno() == -1:
                print(f"Client {self.client_id}: Eroare (get_applications_list): Socket-ul nu este conectat sau este închis.")
                return []
            self.socket.sendall(json.dumps(request).encode('utf-8'))
            response = self.receive_json()
            if response and response.get('status') == 'success':
                return response.get('apps', [])
            else:
                error_msg = response.get('message') if response else "Răspuns invalid sau gol de la server (get_applications_list)."
                print(f"Client {self.client_id}: Eroare la obținerea listei de aplicații: {error_msg}")
                return []
        except socket.error as se:
            print(f"Client {self.client_id}: Eroare socket la get_applications_list: {se}")
            self.stop_event.set()
            return []
        except Exception as e:
            print(f"Client {self.client_id}: Eroare la trimiterea cererii pentru lista de aplicații: {e}")
            return []
        finally:
            self.socket_lock.release()

    def download_application(self, app_name, is_update_download=False, new_version_for_staging=None):
        request = {'command': 'download_app', 'app_name': app_name}
        self.socket_lock.acquire()
        original_socket_timeout = None
        temp_path = os.path.join(self.downloads_dir, f"{app_name}.{os.getpid()}.tmp")
        app_final_path = os.path.join(self.downloads_dir, app_name)
        bytes_received_for_error_reporting = 0
        server_version_from_metadata = None
        staged_file_path = None
        operation_status = {'status': 'failed', 'path': None, 'version': None} # Default
        metadata = {}

        try:
            if not self.socket or (hasattr(self.socket, '_closed') and self.socket._closed) or self.socket.fileno() == -1:
                print(f"Client {self.client_id}: Eroare descărcare {app_name}: Socket-ul nu este conectat sau este închis.")
                return operation_status

            original_socket_timeout = self.socket.gettimeout()

            self.socket.sendall(json.dumps(request).encode('utf-8'))
            metadata = self.receive_json()

            if not metadata or metadata.get('status') == 'error':
                error_msg = metadata.get('message') if metadata else "Răspuns invalid pentru metadate."
                print(f"Client {self.client_id}: Eroare la primirea metadatelor pentru {app_name}: {error_msg}")
                if original_socket_timeout is not None and self.socket and self.socket.fileno() != -1: self.socket.settimeout(original_socket_timeout)
                return operation_status

            file_size = metadata['size']
            server_version_from_metadata = metadata['version']
            operation_status['version'] = server_version_from_metadata
            bytes_received = 0
            bytes_received_for_error_reporting = 0

            print(f"Client {self.client_id}: Începe descărcarea {app_name} (Server v{server_version_from_metadata}, {file_size} bytes)")
            self.socket.settimeout(10.0)
            self.socket.sendall('READY'.encode('utf-8'))

            self.socket.settimeout(60.0)
            with open(temp_path, 'wb') as f:
                last_progress_display_time = time.time()

                while bytes_received < file_size:
                    if self.stop_event.is_set():
                        print(f"Client {self.client_id}: Descărcare anulată din cauza opririi clientului.")
                        raise OperationAborted("Client shutdown during download")

                    try:
                        remaining_bytes = file_size - bytes_received
                        chunk_size_to_receive = min(8192, remaining_bytes)
                        chunk = self.socket.recv(chunk_size_to_receive)
                        if not chunk:
                            if bytes_received < file_size:
                                print(f"\nClient {self.client_id}: Eroare: Conexiunea s-a închis prematur de către server în timpul descărcării. Primit {bytes_received}/{file_size} bytes.")
                                raise EOFError(f"Conexiune închisă prematur. Primit {bytes_received}/{file_size} bytes.")
                            break

                        f.write(chunk)
                        bytes_received += len(chunk)
                        bytes_received_for_error_reporting = bytes_received

                        current_time = time.time()
                        if current_time - last_progress_display_time >= 0.5 or bytes_received == file_size:
                            progress = (bytes_received / file_size) * 100
                            print(f"\rClient {self.client_id}: Progres descărcare: {progress:.1f}% ({bytes_received}/{file_size} bytes)      ",
                                  end='', flush=True)
                            last_progress_display_time = current_time
                    except socket.timeout:
                        print(
                            f"\nClient {self.client_id}: Timeout (60s per chunk) la descărcarea datelor pentru {app_name}. {bytes_received_for_error_reporting}/{file_size} bytes primiți. Reîncercați descărcarea.")
                        raise

            print()
            if bytes_received != file_size:
                 error_message_incomplete = f"Client {self.client_id}: Descărcare incompletă pentru {app_name}. Primit {bytes_received}/{file_size} bytes."
                 print(error_message_incomplete)
                 raise ValueError(error_message_incomplete) # Treat as an error

            print(f"Client {self.client_id}: Verificare dimensiune fișier descărcat...")
            actual_file_size = os.path.getsize(temp_path)
            if actual_file_size != file_size:
                size_mismatch_msg = f"Client {self.client_id}: Dimensiunea fișierului descărcat ({actual_file_size}) nu corespunde cu cea așteptată ({file_size}) pentru {app_name}. Fișier posibil corupt."
                print(size_mismatch_msg)
                raise ValueError(size_mismatch_msg)

            self.socket.settimeout(10.0)
            self.socket.sendall('DONE'.encode('utf-8'))
            print(f"Client {self.client_id}: Confirmare 'DONE' trimisă la server pentru {app_name}.")

            if os.path.exists(app_final_path):
                os.remove(app_final_path)
            os.rename(temp_path, app_final_path)

            self.downloaded_apps[app_name] = server_version_from_metadata # Should be under self.lock if accessed by multiple threads
            print(f"Client {self.client_id}: Aplicația {app_name} (v{server_version_from_metadata}) descărcată și verificată: {app_final_path}.")

            if os.name != 'nt':
                try:
                    os.chmod(app_final_path, 0o755)
                    print(f"Client {self.client_id}: Permisiuni de execuție setate pentru {app_final_path}.")
                except Exception as e_chmod:
                    print(f"Client {self.client_id}: Avertisment: Nu s-au putut seta permisiunile de execuție: {e_chmod}")

            if original_socket_timeout is not None and self.socket and self.socket.fileno() != -1: self.socket.settimeout(original_socket_timeout)
            operation_status.update({'status': 'success', 'path': app_final_path})
            return operation_status

        # Consolidate exception handling for download_application
        except (socket.error, EOFError, ValueError, OperationAborted) as specific_e:
            print(f"\nClient {self.client_id}: Eroare specifică la descărcarea {app_name} ({bytes_received_for_error_reporting}/{metadata.get('size', 'N/A')} bytes): {specific_e}")
            operation_status['status'] = 'failed'
        except Exception as general_e:
            print(f"\nClient {self.client_id}: Eroare generală la descărcarea {app_name} ({bytes_received_for_error_reporting}/{metadata.get('size', 'N/A')} bytes): {general_e}")
            operation_status['status'] = 'failed'
        finally:
            if original_socket_timeout is not None and self.socket and self.socket.fileno() != -1:
                try:
                    self.socket.settimeout(original_socket_timeout)
                except socket.error as e_restore_timeout:
                    print(f"Client {self.client_id}: Avertisment: nu s-a putut restaura timeout-ul socket-ului: {e_restore_timeout}")

            if os.path.exists(temp_path):
                error_occurred = 'specific_e' in locals() or 'general_e' in locals()
                keep_temp_file = False
                if error_occurred:
                    current_exception = locals().get('specific_e') or locals().get('general_e')
                    if isinstance(current_exception, ValueError) and ("Dimensiunea fișierului" in str(current_exception) or "Descărcare incompletă" in str(current_exception)):
                        keep_temp_file = True
                        print(f"Client {self.client_id}: Fișierul temporar {temp_path} NU a fost șters din cauza erorii de conținut/dimensiune, pentru inspecție.")

                if not keep_temp_file:
                    try:
                        os.remove(temp_path)
                        print(f"Client {self.client_id}: Fișier temporar {temp_path} șters.")
                    except OSError as ose:
                        print(f"Client {self.client_id}: Nu s-a putut șterge fișierul temporar {temp_path}: {ose}")

            self.socket_lock.release()
        return operation_status

    def run_application(self, app_name):
        app_path_relative = os.path.join(self.downloads_dir, app_name)
        app_path_absolute = os.path.abspath(app_path_relative)

        if app_name not in self.downloaded_apps:
            print(f"Client {self.client_id}: Aplicația '{app_name}' nu a fost descărcată sau informațiile despre versiune lipsesc.")
            print(f"  Puteți încerca să o descărcați folosind comanda 'download {app_name}'.")
            return

        if not os.path.exists(app_path_absolute):
            print(f"Client {self.client_id}: Fișierul aplicației '{app_name}' nu a fost găsit la '{app_path_absolute}'.")
            del self.downloaded_apps[app_name]
            return

        try:
            if self.is_app_running(app_name):
                print(f"Client {self.client_id}: Aplicația '{app_name}' rulează deja (PID: {self.running_apps[app_name].pid}).")
                return

            print(f"Client {self.client_id}: Se încearcă pornirea aplicației '{app_name}' de la '{app_path_absolute}'...")

            current_process = None
            if sys.platform == "win32":
                os.startfile(app_path_absolute)
                print(f"Client {self.client_id}: Aplicația '{app_name}' a fost pornită folosind os.startfile.")
            elif sys.platform == "darwin": # macOS
                if app_name.lower().endswith((".dmg", ".app")):
                     print(f"Client {self.client_id}: Se utilizează 'open' pentru {app_name} pe macOS.")
                     current_process = subprocess.Popen(['open', app_path_absolute])
                else:
                    if not os.access(app_path_absolute, os.X_OK):
                        print(f"Client {self.client_id}: Aplicația '{app_name}' nu are permisiuni de execuție. Se încearcă setarea (chmod +x)...")
                        os.chmod(app_path_absolute, 0o755)
                    current_process = subprocess.Popen([app_path_absolute])
            else:
                if not os.access(app_path_absolute, os.X_OK):
                    print(f"Client {self.client_id}: Aplicația '{app_name}' nu are permisiuni de execuție. Se încearcă setarea (chmod +x)...")
                    os.chmod(app_path_absolute, 0o755)
                current_process = subprocess.Popen([app_path_absolute])

            if current_process:
                with self.lock:
                    self.running_apps[app_name] = current_process
                print(f"Client {self.client_id}: Aplicația '{app_name}' a fost pornită (PID: {current_process.pid}).")

        except FileNotFoundError:
            print(f"Client {self.client_id}: Eroare critică: Fișierul aplicației '{app_name}' nu a fost găsit la '{app_path_absolute}' deși verificarea inițială a trecut.")
            if app_name in self.downloaded_apps: del self.downloaded_apps[app_name]
        except PermissionError:
            print(f"Client {self.client_id}: Eroare de permisiuni la rularea '{app_name}'. Asigurați-vă că aveți drepturile necesare.")
        except OSError as e:
            if e.errno == errno.ENOEXEC: # Exec format error
                print(f"Client {self.client_id}: Eroare de format la executarea '{app_name}' (Exec format error).")
                print(f"  Verificați dacă '{app_name}' este un executabil compatibil cu sistemul dvs. ({sys.platform}).")
                if sys.platform == "darwin" and not app_name.lower().endswith((".dmg", ".app")) :
                    print("  Pe macOS, pentru script-uri sau binare simple, asigurați-vă că au shebang (ex: #!/bin/bash) și permisiuni de execuție.")
                    print("  Pentru aplicații .app sau .dmg, acestea ar trebui gestionate corect.")
                elif sys.platform.startswith("linux"):
                     print("  Pe Linux, asigurați-vă că este un binar ELF compilat pentru arhitectura corectă sau un script valid cu shebang.")
            else:
                print(f"Client {self.client_id}: Eroare la rularea aplicației '{app_name}': {e}")
        except Exception as e:
            print(f"Client {self.client_id}: Eroare necunoscută la rularea aplicației '{app_name}': {e}")

    def is_app_running(self, app_name):
        """Verifică dacă o aplicație (pornită cu Popen) rulează."""
        with self.lock:
            process = self.running_apps.get(app_name)
            if process:
                if process.poll() is None:
                    return True
                else:
                    del self.running_apps[app_name]
                    print(f"Client {self.client_id}: Procesul pentru {app_name} (PID: {process.pid}) s-a încheiat (cod: {process.returncode}). Eliminat din lista aplicațiilor active.")
                    return False
        return False

    def terminate_app(self, app_name):
        """ Oprește o aplicație care rulează (doar cele pornite cu Popen). """
        if sys.platform == "win32" and not self.running_apps.get(app_name):
             print(f"Client {self.client_id}: (terminate_app) Pe Windows, aplicațiile pornite cu startfile nu pot fi oprite programatic de client în acest mod.")
             return False


        if self.is_app_running(app_name):
            with self.lock:
                process = self.running_apps.get(app_name)
                if process:
                    try:
                        print(f"Client {self.client_id}: Se încearcă oprirea aplicației {app_name} (PID: {process.pid})...")
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                            print(f"Client {self.client_id}: Aplicația {app_name} (PID: {process.pid}) a fost oprită (terminate).")
                        except subprocess.TimeoutExpired:
                            print(f"Client {self.client_id}: Aplicația {app_name} (PID: {process.pid}) nu s-a oprit în 5 secunde după terminate. Se forțează (kill)...")
                            process.kill()
                            process.wait(timeout=5) # Wait for kill
                            print(f"Client {self.client_id}: Aplicația {app_name} (PID: {process.pid}) a fost forțată să se oprească (kill).")

                        if app_name in self.running_apps:
                            del self.running_apps[app_name]
                        return True
                    except Exception as e:
                        print(f"Client {self.client_id}: Eroare la oprirea aplicației {app_name}: {e}")
                        return False
                else:
                    print(f"Client {self.client_id}: (terminate_app) Aplicația {app_name} s-a încheiat înainte de a putea fi oprită explicit.")
                    return True
        else:
            print(f"Client {self.client_id}: (terminate_app) Aplicația {app_name} nu rulează sau nu este gestionată (nu a fost pornită cu Popen).")
            return False

    def update_application(self, app_name, notification_data):
        new_server_version = notification_data['version']
        print(f"\nClient {self.client_id}: Notificare actualizare pentru {app_name} la v{new_server_version}. Dimensiune: {notification_data['size']}B.")
        with self.lock:
            current_local_version = self.downloaded_apps.get(app_name)
        print(f"Client {self.client_id}: Versiune locală {app_name}: v{current_local_version if current_local_version else 'N/A'}")

        if current_local_version and current_local_version >= new_server_version:
            print(f"Client {self.client_id}: {app_name} v{current_local_version} este deja actualizat/mai nou. Nu se descarcă.")
            print("\n--- Meniu Client ---")
            print("1. Listează aplicațiile disponibile (cu versiuni)")
            print("2. Descarcă o aplicație")
            print("3. Rulează o aplicație descărcată")
            print("4. Listează aplicațiile pornite local (monitorizate)")
            print("5. Ieșire")
            print(f"Client {self.client_id}: Procesul de actualizare pentru {app_name} s-a încheiat. Introduceți o comandă:")
            return

        print(f"Client {self.client_id}: Se descarcă noua versiune {app_name} v{new_server_version}...")
        download_result = self.download_application(app_name, is_update_download=True, new_version_for_staging=new_server_version)

        if download_result['status'] == 'success':
            print(f"Client {self.client_id}: {app_name} actualizat cu succes la v{download_result['version']} în {download_result['path']}.")
            with self.lock:
                if app_name in self.running_apps:
                    print(f"Client {self.client_id}: {app_name} rula. Reporniți manual pentru a folosi noua versiune.")
        elif download_result['status'] == 'staged':
            print(f"Client {self.client_id}: {app_name} v{download_result['version']} descărcat și salvat temporar. Se încearcă aplicarea actualizării...")
            self.handle_staged_update(app_name, download_result['path'], download_result['version'])
        else:
            print(f"Client {self.client_id}: Eroare la descărcarea actualizării pentru {app_name}.")

        print(f"Client {self.client_id}: Procesul de actualizare pentru {app_name} s-a încheiat. Introduceți o comandă:")

    def handle_staged_update(self, app_name, staged_file_path, new_version_timestamp):
        print(f"Client {self.client_id}: Gestionare actualizare în scenă pentru {app_name} (noua versiune: {new_version_timestamp}). Fișier în scenă: {staged_file_path}")
        final_app_path = os.path.join(self.downloads_dir, app_name)
        max_retries = 10
        retry_delay = 5

        for attempt in range(max_retries):
            if self.stop_event.is_set():
                print(f"Client {self.client_id}: (handle_staged_update) Oprire solicitată, se anulează încercările de actualizare pentru {app_name}.")
                return

            app_was_running = self.is_app_running(app_name) # Check before trying to terminate
            if app_was_running:
                print(f"Client {self.client_id}: (Attempt {attempt + 1}/{max_retries}) Aplicația {app_name} rulează. Se încearcă oprirea...")
                self.terminate_app(app_name)
                time.sleep(1)
                if self.is_app_running(app_name):
                    print(f"Client {self.client_id}: (Attempt {attempt + 1}/{max_retries}) Nu s-a putut opri {app_name}. Se reîncearcă în {retry_delay}s...")
                    time.sleep(retry_delay)
                    continue
            try:
                print(f"Client {self.client_id}: (Attempt {attempt + 1}/{max_retries}) Se încearcă înlocuirea {final_app_path} cu {staged_file_path}...")
                if not os.path.exists(staged_file_path):
                    print(f"Client {self.client_id}: EROARE CRITICĂ: Fișierul în scenă {staged_file_path} nu există! Actualizare eșuată pentru {app_name}.")
                    return

                os.replace(staged_file_path, final_app_path)
                with self.lock:
                    self.downloaded_apps[app_name] = new_version_timestamp
                print(f"Client {self.client_id}: Actualizare reușită pentru {app_name} la versiunea {new_version_timestamp} folosind fișierul din scenă.")
                print(f"  Fișierul {staged_file_path} a fost mutat în {final_app_path}.")
                self.socket_lock.acquire()
                try:
                    if self.socket and self.socket.fileno() != -1:
                        print(f"Client {self.client_id}: Se trimite \'DONE\' la server pentru actualizarea {app_name} (v{new_version_timestamp}).")
                        self.socket.sendall('DONE'.encode('utf-8'))
                    else:
                        print(f"Client {self.client_id}: (handle_staged_update) Socket închis, nu se poate trimite 'DONE'.")
                except Exception as e_send_done:
                    print(f"Client {self.client_id}: Eroare la trimiterea \'DONE\' după actualizarea din scenă: {e_send_done}")
                finally:
                    self.socket_lock.release()
                return

            except PermissionError as pe:
                print(f"Client {self.client_id}: (Attempt {attempt + 1}/{max_retries}) Eroare de permisiune la înlocuirea {app_name}: {pe}. Fișierul este probabil încă blocat.")
            except Exception as e:
                print(f"Client {self.client_id}: (Attempt {attempt + 1}/{max_retries}) Eroare la înlocuirea {app_name}: {e}")

            print(f"Client {self.client_id}: (Attempt {attempt + 1}/{max_retries}) Reîncercare în {retry_delay} secunde...")
            time.sleep(retry_delay)

        print(f"Client {self.client_id}: Eșec la actualizarea {app_name} după {max_retries} încercări.")
        print(f"  Fișierul actualizat este încă la: {staged_file_path}")
        print(f"  Puteți încerca să închideți manual aplicația '{app_name}' și apoi să mutați '{staged_file_path}' în '{final_app_path}'.")

    def handle_forced_app_update(self, app_name, new_version_server, app_size_server):
        print(f"Client {self.client_id}: Primită notificare de actualizare FORȚATĂ pentru {app_name} la versiunea server {new_version_server}.")
        local_app_path = os.path.join(self.downloads_dir, app_name)

        if self.is_app_running(app_name):
            print(f"Client {self.client_id}: (ForcedUpdate) Aplicația {app_name} rulează. Se încearcă oprirea...")
            terminated_successfully = self.terminate_app(app_name)
            if terminated_successfully:
                print(f"Client {self.client_id}: (ForcedUpdate) Aplicația {app_name} a fost oprită.")
                time.sleep(0.5) # Scurtă pauză pentru eliberarea resurselor
            else:
                print(f"Client {self.client_id}: (ForcedUpdate) AVERTISMENT: Nu s-a putut opri {app_name}. Ștergerea și actualizarea ar putea eșua.")
        
        deleted_local_copy = False
        if os.path.exists(local_app_path):
            print(f"Client {self.client_id}: (ForcedUpdate) Se șterge versiunea locală a {app_name} de la {local_app_path}...")
            try:
                os.remove(local_app_path)
                print(f"Client {self.client_id}: (ForcedUpdate) Versiunea locală a {app_name} a fost ștearsă.")
                deleted_local_copy = True
                with self.lock: # Remove from downloaded_apps as it's now deleted
                    if app_name in self.downloaded_apps:
                        del self.downloaded_apps[app_name]
            except PermissionError as pe:
                 print(f"Client {self.client_id}: (ForcedUpdate) Eroare de PERMISIUNE la ștergerea {local_app_path}: {pe}. Fișierul ar putea fi încă blocat.")
            except Exception as e:
                print(f"Client {self.client_id}: (ForcedUpdate) Eroare la ștergerea {local_app_path}: {e}")
        else:
            print(f"Client {self.client_id}: (ForcedUpdate) Versiunea locală a {app_name} nu există la {local_app_path}. Se continuă cu descărcarea.")
            deleted_local_copy = True

        if not deleted_local_copy:
            print(f"Client {self.client_id}: (ForcedUpdate) EȘEC la ștergerea versiunii locale a {app_name}. Actualizarea forțată nu poate continua în siguranță.")
            print(f"  Vă rugăm închideți manual aplicația și ștergeți '{local_app_path}', apoi reporniți clientul sau încercați o descărcare manuală.")
            return

        print(f"Client {self.client_id}: (ForcedUpdate) Se încearcă descărcarea noii versiuni ({new_version_server}) pentru {app_name}.")
        download_result = self.download_application(app_name)

        if download_result and download_result.get('status') == 'success':
            new_local_version = download_result.get('version')
            new_local_path = download_result.get('path')
            print(f"Client {self.client_id}: (ForcedUpdate) {app_name} actualizat cu succes la versiunea {new_local_version} ({new_local_path}).")
        elif download_result and download_result.get('status') == 'staged':
            staged_path = download_result.get('path')
            staged_version = download_result.get('version')
            print(f"Client {self.client_id}: (ForcedUpdate) Descărcarea pentru {app_name} a rezultat într-un fișier în scenă la {staged_path} (v{staged_version}).")
            print(f"  Se va încerca gestionarea actualizării din scenă...")
            self.handle_staged_update(app_name, staged_path, staged_version)
        else:
            print(f"Client {self.client_id}: (ForcedUpdate) EȘEC la descărcarea noii versiuni pentru {app_name}.")

    def listen_for_notifications(self):

        print(f"Client {self.client_id}: Thread-ul de notificări a pornit.")
        temp_buffer = b""

        while not self.stop_event.is_set():
            acquired_lock_for_notif = self.socket_lock.acquire(blocking=False)
            if not acquired_lock_for_notif:
                time.sleep(0.1)
                continue

            try:
                if not self.socket or self.socket.fileno() == -1:
                    if not self.stop_event.is_set():
                        print(f"Client {self.client_id} (Notificări): Socket invalid sau închis. Thread-ul de notificări se oprește.")
                    self.stop_event.set()
                    break

                self.socket.settimeout(1.0)
                
                chunk = self.socket.recv(4096)

                if not chunk:
                    if not self.stop_event.is_set():
                        print(f"Client {self.client_id} (Notificări): Serverul a închis conexiunea. Thread-ul de notificări se oprește.")
                    self.stop_event.set()
                    break
                
                temp_buffer += chunk
                
                while True:
                    try:
                        decoded_buffer = temp_buffer.decode('utf-8', errors='replace')

                        try:
                            message = json.loads(decoded_buffer)
                            temp_buffer = b""
                        except json.JSONDecodeError:
                            break

                        print(f"\nClient {self.client_id}: Notificare/Mesaj primit de la server: {message}")

                        msg_type = message.get('type')
                        app_name_notif = message.get('app_name')

                        if msg_type == 'app_update':
                            print(f"Client {self.client_id}: Notificare de actualizare primită pentru {app_name_notif} (Versiune server: {message.get('version')}).")
                            self.update_application(app_name_notif, message)
                        elif msg_type == 'force_delete_then_redownload':
                            print(f"Client {self.client_id}: Notificare de ACTUALIZARE FORȚATĂ primită pentru {app_name_notif} (Versiune server: {message.get('version')}).")
                            self.handle_forced_app_update(app_name_notif, message.get('version'), message.get('size'))
                        else:
                            print(f"Client {self.client_id}: Tip de notificare necunoscut: {msg_type}")

                    except UnicodeDecodeError as ude:
                        print(f"\nClient {self.client_id} (Notificări): Eroare decodare Unicode în bufferul de notificări: {ude}. Buffer: {temp_buffer[:100]}...")
                        print(f"  Acest lucru NU ar trebui să se întâmple dacă mecanismul de blocare al socket-ului (socket_lock) funcționează corect.")
                        print(f"  Este posibil ca datele binare ale unui fișier să fi fost interceptate.")
                        print(f"  Se goleşte bufferul de notificări pentru a încerca recuperarea.")
                        temp_buffer = b""
                        break
                    
                    except json.JSONDecodeError as je:
                        break

                    if not temp_buffer:
                        break


            except socket.timeout:
                pass
            except BlockingIOError as bio:
                if bio.errno == errno.EAGAIN or bio.errno == errno.EWOULDBLOCK:
                    time.sleep(0.05)
                    pass
                else:
                    print(f"\nClient {self.client_id} (Notificări): Eroare BlockingIOError: {bio}. Thread-ul se oprește.")
                    self.stop_event.set()
                    break
            except socket.error as se:
                if not self.stop_event.is_set():
                    print(f"\nClient {self.client_id} (Notificări): Eroare socket: {se}. Thread-ul se oprește.")
                self.stop_event.set()
                break
            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"\nClient {self.client_id} (Notificări): Eroare neașteptată: {e}. Thread-ul se oprește.")
                    import traceback
                    traceback.print_exc()
                self.stop_event.set()
                break
            finally:
                if acquired_lock_for_notif:
                    self.socket_lock.release()
        
        print(f"Client {self.client_id}: Thread-ul de notificări s-a oprit.")

    def close_connection(self):
        print("Se închide conexiunea...")
        self.stop_event.set()
        lock_acquired_for_close = self.socket_lock.acquire(timeout=0.1)

        try:
            if self.notification_thread and self.notification_thread.is_alive():
                if threading.current_thread() != self.notification_thread:
                    print("Așteptare oprire thread notificare (max 5s)...")
                    self.notification_thread.join(timeout=5)
                    if self.notification_thread.is_alive():
                        print("Thread-ul de notificare nu s-a oprit la timp.")

            if self.socket:
                socket_active = True
                try:
                    if self.socket.fileno() == -1:
                        socket_active = False
                except (socket.error, AttributeError):
                    socket_active = False

                if socket_active:
                    try:
                        self.socket.shutdown(socket.SHUT_RDWR)
                    except (socket.error, OSError):
                        pass # Common if already closed
                    finally:
                        try:
                            self.socket.close()
                        except (socket.error, OSError):
                            pass
                self.socket = None
                print("Socket-ul clientului a fost marcat ca închis.")

        finally:
            if lock_acquired_for_close:
                self.socket_lock.release()
            elif self.socket_lock.locked() and threading.current_thread() != self.notification_thread:
                 pass


class OperationAborted(Exception):
    pass


def main():
    client = ApplicationClient()
    try:
        client.connect()

        def display_menu_and_prompt():
            print("\n--- Meniu Client ---")
            print("1. Listează aplicațiile disponibile (cu versiuni)")
            print("2. Descarcă o aplicație")
            print("3. Rulează o aplicație descărcată")
            print("4. Listează aplicațiile pornite local (monitorizate)")
            print("5. Ieșire")
            return input("Introduceți opțiunea: ").strip()

        while not client.stop_event.is_set():
            choice = display_menu_and_prompt()

            if choice == '1':
                apps_with_versions = client.get_applications_list()
                if apps_with_versions:
                    print("\nAplicații disponibile pe server:")
                    for app_info in apps_with_versions:
                        print(f"  - {app_info['name']} (Versiune server: {app_info['version']})")
                else: print("Nicio aplicație disponibilă sau eroare la listare.")
            elif choice == '2':
                app_name = input("Numele aplicației de descărcat: ").strip()
                if app_name: client.download_application(app_name)
                else: print("Numele aplicației nu poate fi gol.")
            elif choice == '3':
                app_name = input("Numele aplicației de rulat: ").strip()
                if app_name: client.run_application(app_name)
                else: print("Numele aplicației nu poate fi gol.")
            elif choice == '4':
                client.list_running_apps()
            elif choice == '5':
                print("Se închide clientul...")
                break
            else: print("Opțiune invalidă.")

    except ConnectionRefusedError:
        print(
            "EROARE CRITICĂ: Conexiunea la server a fost refuzată. Verificați dacă serverul rulează și este accesibil.")
    except KeyboardInterrupt:
        print("\nClient închis de utilizator.")
    except Exception as e:
        print(f"Eroare neașteptată în client: {e}")
    finally:
        if hasattr(client, 'close_connection'):
            client.close_connection()
        elif hasattr(client, 'socket') and client.socket:
            try:
                client.socket.close()
                print("Socket-ul clientului (fallback) închis.")
            except Exception as e_sock:
                print(f"Eroare la închiderea socket-ului (fallback): {e_sock}")
        print("Clientul s-a oprit.")


if __name__ == '__main__':
    main()