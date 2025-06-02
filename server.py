import socket
import threading
import os
import json
import time
import shutil


class ApplicationServer:

    def __init__(self, host='localhost', port=5000):
        self.host = host
        self.port = port
        self.applications = {}
        self.client_download_versions = {}
        self.lock = threading.Lock()
        self.active_clients = {} 
        self.stop_server_event = threading.Event()
        self.load_applications()

    def load_applications(self):
        apps_dir = 'apps'
        if not os.path.exists(apps_dir):
            os.makedirs(apps_dir)
            print(f"Directorul '{apps_dir}' a fost creat.")

        with self.lock:
            newly_loaded_apps = {}
            for file_name in os.listdir(apps_dir):
                app_full_path = os.path.join(apps_dir, file_name)
                if os.path.isfile(app_full_path):
                    try:
                        version = os.path.getmtime(app_full_path)
                        newly_loaded_apps[file_name] = {
                            'name': file_name,
                            'path': app_full_path,
                            'version': version
                        }
                        if file_name not in self.applications or self.applications[file_name]['version'] != version:
                             print(f"Aplicație (re)încărcată/actualizată în listă: {file_name} (Versiune: {version})")
                        self.applications[file_name] = newly_loaded_apps[file_name]
                    except Exception as e:
                        print(f"Eroare la încărcarea metadatelor pentru {file_name}: {e}")
            current_app_files = set(newly_loaded_apps.keys())
            apps_to_remove = []
            for app_name_in_mem in self.applications.keys():
                if app_name_in_mem not in current_app_files:
                    apps_to_remove.append(app_name_in_mem)
            for app_to_remove in apps_to_remove:
                print(f"Aplicația {app_to_remove} nu mai există în directorul 'apps'. Se elimină din listă.")
                del self.applications[app_to_remove]

    def _send_update_notifications(self, app_name, new_version, new_size):
        clients_to_notify_sockets = []
        if not self.active_clients:
            print(f"Info Notificare ({app_name}): Niciun client activ pentru a notifica.")
            return

        potential_recipients_count = 0

        for client_sock, client_data in list(self.active_clients.items()):
            client_address = client_data['address']
            current_client_app_versions = client_data['downloaded_app_versions']
            client_downloaded_this_app_version = current_client_app_versions.get(app_name)
            
            potential_recipients_count += 1

            if client_downloaded_this_app_version is not None:
                if client_downloaded_this_app_version < new_version:
                    print(f"Info Notificare ({app_name}): Clientul {client_address} (v{client_downloaded_this_app_version}) necesită actualizare forțată la v{new_version}. Se adaugă la lista de notificare.")
                    clients_to_notify_sockets.append(client_sock)
                else:
                    print(f"Info Notificare ({app_name}): Clientul {client_address} (v{client_downloaded_this_app_version}) are deja versiunea {new_version} sau mai nouă. Nu se notifică.")
            else:
                 print(f"Info Notificare ({app_name}): Clientul {client_address} nu a descărcat anterior această aplicație. Nu se notifică pentru actualizare.")
        
        if not clients_to_notify_sockets:
            print(f"Info Notificare ({app_name}): Din {potential_recipients_count} clienți activi, niciunul nu necesită notificare pentru această actualizare forțată.")
            return

        notification_message = {
            'type': 'force_delete_then_redownload',
            'app_name': app_name,
            'version': new_version,
            'size': new_size
        }
        print(f"Info Notificare ({app_name}): Se notifică {len(clients_to_notify_sockets)} clienți pentru actualizare forțată...")
        for sock_to_notify in clients_to_notify_sockets:
            notify_addr_str = str(self.active_clients.get(sock_to_notify, {}).get('address', 'Adresă necunoscută'))
            try:
                self._send_json_response(sock_to_notify, notification_message)
                print(f"Notificare de actualizare forțată pentru {app_name} trimisă clientului {notify_addr_str}")
            except Exception as e_notify:
                print(f"Eroare la trimiterea notificării de update forțat către {notify_addr_str}: {e_notify}")

    def _periodic_app_update_checker(self):
        print("Monitorizare periodică a actualizărilor de aplicații pornită (verificare la 2 secunde).")
        apps_dir = 'apps'
        if not os.path.exists(apps_dir):
            print(f"EROARE CRITICĂ: Directorul '{apps_dir}' nu există. Monitorizarea actualizărilor nu poate funcționa.")
            return

        while not self.stop_server_event.is_set():
            try:
                current_disk_apps = {}
                for file_name in os.listdir(apps_dir):
                    app_full_path = os.path.join(apps_dir, file_name)
                    if os.path.isfile(app_full_path):
                        try:
                            current_disk_apps[file_name] = {
                                'path': app_full_path,
                                'version': os.path.getmtime(app_full_path),
                                'size': os.path.getsize(app_full_path)
                            }
                        except FileNotFoundError:
                            continue 
                        except Exception as e_scan:
                            print(f"Eroare la scanarea fișierului {file_name} în cron: {e_scan}")
                            continue
                
                with self.lock:
                    for app_name, disk_app_info in current_disk_apps.items():
                        current_disk_version = disk_app_info['version']
                        current_disk_size = disk_app_info['size']
                        
                        if app_name in self.applications:
                            if current_disk_version > self.applications[app_name]['version']:
                                old_mem_version = self.applications[app_name]['version']
                                self.applications[app_name]['version'] = current_disk_version
                                self.applications[app_name]['path'] = disk_app_info['path']
                                print(f"Cron: Actualizare detectată pentru {app_name}. Versiune server: {old_mem_version} -> {current_disk_version}")
                                self._send_update_notifications(app_name, current_disk_version, current_disk_size)
                        else:
                            self.applications[app_name] = disk_app_info.copy()
                            newly_added_version = disk_app_info['version']
                            newly_added_size = disk_app_info['size']
                            print(f"Cron: Aplicație nouă detectată și adăugată: {app_name} (Versiune: {newly_added_version})")
                            print(f"Cron: Aplicația {app_name} este nouă/înlocuită pe disc. Se verifică dacă este o actualizare pentru clienți...")
                            self._send_update_notifications(app_name, newly_added_version, newly_added_size)

                    app_names_on_disk = set(current_disk_apps.keys())
                    apps_to_remove_from_memory = []
                    for mem_app_name in self.applications.keys():
                        if mem_app_name not in app_names_on_disk:
                            apps_to_remove_from_memory.append(mem_app_name)
                    
                    for app_to_remove in apps_to_remove_from_memory:
                        print(f"Cron: Aplicația {app_to_remove} a fost ștearsă din director. Se elimină din memoria serverului.")
                        del self.applications[app_to_remove]


            except Exception as e_cron_loop:
                print(f"Eroare în bucla de monitorizare actualizări aplicații: {e_cron_loop}")
            
            time.sleep(2)
        print("Monitorizare periodică a actualizărilor de aplicații oprită.")

    def start(self):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.port))
        server_socket.listen(5)
        print(f"Server pornit pe {self.host}:{self.port}. Apăsați Ctrl+C pentru a opri.")

        self.stop_server_event.clear()
        checker_thread = threading.Thread(target=self._periodic_app_update_checker, name="AppUpdateChecker")
        checker_thread.daemon = True
        checker_thread.start()

        try:
            while True:
                client_socket, address = server_socket.accept()
                print(f"Nouă conexiune de la {address}")
                with self.lock:
                    previous_downloads_for_address = self.client_download_versions.get(address, {})
                    self.active_clients[client_socket] = {
                        'address': address,
                        'downloaded_app_versions': previous_downloads_for_address.copy() 
                    }
                client_thread = threading.Thread(target=self.handle_client, args=(client_socket, address))
                client_thread.daemon = True
                client_thread.start()
        except KeyboardInterrupt:
            print("Serverul se oprește (Ctrl+C primit)...")
        except Exception as e:
            print(f"Eroare la acceptarea conexiunii: {e}")
        finally:
            print("Se oprește monitorizarea actualizărilor și se închide serverul...")
            self.stop_server_event.set()
            if 'checker_thread' in locals() and checker_thread.is_alive():
                checker_thread.join(timeout=5)
            server_socket.close()
            print("Socket-ul serverului a fost închis.")

    def _send_json_response(self, client_socket, data_dict):
        """ Trimite un răspuns JSON către client. """
        try:
            client_socket.sendall(json.dumps(data_dict).encode('utf-8'))
        except Exception as e:
            print(f"Eroare la trimiterea răspunsului JSON: {e}")

    def handle_client(self, client_socket, address):
        print(f"Manipulare client {address}")
        try:
            while True:
                client_socket.settimeout(300.0)
                data_received_bytes = client_socket.recv(4096)
                if not data_received_bytes:
                    print(f"Clientul {address} s-a deconectat (nu s-au primit date).")
                    break
                client_socket.settimeout(None)

                try:
                    request_str = data_received_bytes.decode('utf-8')
                    request = json.loads(request_str)
                    print(f"Cerere JSON primită de la {address}: {request_str}")
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    print(f"Eroare la decodarea cererii JSON de la {address}: {e}. Date: {data_received_bytes!r}")
                    self._send_json_response(client_socket, {'status': 'error', 'message': 'Cerere JSON invalidă.'})
                    break

                command = request.get('command')
                print(f"Comanda '{command}' primită de la {address}.")

                if command == 'list_apps':
                    with self.lock:
                        apps_list = [{'name': name, 'version': data['version']} for name, data in self.applications.items()]
                    self._send_json_response(client_socket, {'status': 'success', 'apps': apps_list})

                elif command == 'download_app':
                    app_name = request.get('app_name')
                    app_info = None
                    with self.lock:
                        app_info_candidate = self.applications.get(app_name)
                        if app_info_candidate:
                             app_info = app_info_candidate.copy()

                    if app_info:
                        app_file_path = app_info['path']
                        current_app_version = app_info['version'] # This is the timestamp
                        try:
                            with open(app_file_path, 'rb') as f:
                                app_data = f.read()

                            metadata = {
                                'status': 'success',
                                'app_name': app_name,
                                'version': current_app_version,
                                'size': len(app_data)
                            }
                            self._send_json_response(client_socket, metadata)

                            client_socket.settimeout(60.0)
                            ack = client_socket.recv(1024).decode('utf-8', errors='ignore')
                            client_socket.settimeout(None)

                            if ack == 'READY':
                                print(f"Clientul {address} este gata. Se trimite {app_name} (v{current_app_version}, {len(app_data)} bytes)...")
                                client_socket.sendall(app_data)
                                print(f"Fișierul {app_name} trimis complet către {address}.")

                                client_socket.settimeout(60.0)
                                final_ack = client_socket.recv(1024).decode('utf-8', errors='ignore')
                                client_socket.settimeout(None)

                                if final_ack == 'DONE':
                                    with self.lock:
                                        self.client_download_versions.setdefault(address, {})[app_name] = current_app_version
                                        
                                        if client_socket in self.active_clients:
                                            self.active_clients[client_socket]['downloaded_app_versions'][app_name] = current_app_version
                                    print(f"Transferul pentru {app_name} (v{current_app_version}) către {address} confirmat de client.")
                                else:
                                    print(f"Confirmare finală ('{final_ack}') invalidă de la {address} pentru {app_name}.")
                            else:
                                print(f"Clientul {address} nu a trimis 'READY' pentru {app_name}. Răspuns: '{ack}'.")
                        except FileNotFoundError:
                            print(f"Eroare server: Fișierul {app_file_path} negăsit pentru {app_name}.")
                            self._send_json_response(client_socket, {'status': 'error', 'message': f'Fișierul {app_name} nu există.'})
                        except socket.timeout as ste:
                            print(f"Server: Timeout în transfer cu {address} pentru {app_name}: {ste}")
                        except Exception as e_file_transfer:
                            print(f"Server: Eroare la transferul {app_name} către {address}: {e_file_transfer}")
                            try:
                                self._send_json_response(client_socket, {'status': 'error', 'message': f'Eroare server la transfer: {str(e_file_transfer)}'})
                            except Exception: pass # Avoid error cascades if sending error fails
                    else:
                        self._send_json_response(client_socket, {'status': 'error', 'message': f'Aplicația {app_name} nu a fost găsită.'})
                else:
                    self._send_json_response(client_socket, {'status': 'error', 'message': f'Comanda {command} este necunoscută.'})

        except socket.timeout:
            print(f"Timeout în așteptarea datelor de la clientul {address}. Se închide conexiunea.")
        except ConnectionResetError:
            print(f"Clientul {address} a resetat conexiunea.")
        except Exception as e_client_loop:
            print(f"Eroare neașteptată în handle_client pentru {address}: {e_client_loop}")
        finally:
            print(f"Se închide conexiunea cu clientul {address}.")
            with self.lock:
                if client_socket in self.active_clients:
                    session_versions = self.active_clients[client_socket]['downloaded_app_versions']
                    self.client_download_versions[address] = session_versions.copy() 
                    del self.active_clients[client_socket]
            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except: pass
            client_socket.close()

    def update_application(self, app_name, new_version_file_path):
        with self.lock: 
            if not os.path.exists(new_version_file_path) or not os.path.isfile(new_version_file_path):
                print(f"(ManualUpdate) Eroare: Calea '{new_version_file_path}' pt {app_name} nu e validă.")
                return
            if app_name not in self.applications:
                print(f"(ManualUpdate) Eroare: Aplicația {app_name} nu există pe server.")
                return
            try:
                old_path = self.applications[app_name]['path']
                apps_dir = os.path.dirname(old_path)
                if not os.path.exists(apps_dir): os.makedirs(apps_dir)
                temp_staging_path = os.path.join(apps_dir, f"{app_name}.manual_stage_{time.time()}")
                shutil.copy2(new_version_file_path, temp_staging_path)
                os.replace(temp_staging_path, old_path)
                new_server_version = os.path.getmtime(old_path)
                self.applications[app_name]['version'] = new_server_version
                print(f"(ManualUpdate) Aplicația {app_name} actualizată pe disc la versiunea {new_server_version}.")
                print("  Monitorizarea periodică va detecta și notifica clienții.")
            except Exception as e:
                print(f"(ManualUpdate) Eroare majoră la actualizarea {app_name}: {e}")
                if 'temp_staging_path' in locals() and os.path.exists(temp_staging_path):
                    try: os.remove(temp_staging_path); print("(ManualUpdate) Staging file șters.")
                    except OSError: pass
            
if __name__ == '__main__':
    server = ApplicationServer()
    
    def example_manual_update():
        time.sleep(45)
        print("\nADMIN: Simulare actualizare aplicație...")
        
        apps_dir_main = 'apps'
        if not os.path.exists(apps_dir_main): os.makedirs(apps_dir_main)
        
        test_app_name = "DummyApp.txt"
        test_app_path_on_server = os.path.join(apps_dir_main, test_app_name)

        if not os.path.exists(test_app_path_on_server):
            with open(test_app_path_on_server, "w") as f:
                f.write("Versiunea initiala a DummyApp.")
            print(f"ADMIN: Creat {test_app_name} initial.")
            server.load_applications()

        temp_admin_uploads_dir = 'admin_uploads_temp'
        if not os.path.exists(temp_admin_uploads_dir): os.makedirs(temp_admin_uploads_dir)
        
        new_version_content = f"DummyApp Content - Versiunea {time.time()}"
        new_version_source_path = os.path.join(temp_admin_uploads_dir, f"{test_app_name}.new_v_file")
        with open(new_version_source_path, "w") as f:
            f.write(new_version_content)
        print(f"ADMIN: Creat fisier noua versiune: {new_version_source_path}")

        server.update_application(test_app_name, new_version_source_path)
        
        if os.path.exists(new_version_source_path): os.remove(new_version_source_path)
        if os.path.exists(temp_admin_uploads_dir) and not os.listdir(temp_admin_uploads_dir):
            os.rmdir(temp_admin_uploads_dir)

    server.start()
    print("Server oprit complet.")