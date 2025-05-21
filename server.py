import socket
import threading
import os
import json
import time


class ApplicationServer:
    """
    Clasa server care gestionează aplicațiile executabile și conexiunile cu clienții.
    Păstrează lista de aplicații disponibile și ține evidența clienților care au descărcat aplicații.
    """

    def __init__(self, host='localhost', port=5000):
        """
        Inițializează serverul cu setările de host și port.
        Creează structurile necesare pentru urmărirea aplicațiilor și clienților.
        """
        self.host = host
        self.port = port
        self.applications = {}  # Dicționar pentru informații despre aplicații
        self.client_apps = {}  # Dicționar pentru urmărirea clienților și aplicațiile lor
        self.lock = threading.Lock()  # Blocare pentru operații thread-safe
        self.load_applications()

    def load_applications(self):
        """
        Încarcă toate aplicațiile executabile din directorul 'apps'.
        Creează directorul 'apps' dacă nu există.
        """
        apps_dir = 'apps'
        if not os.path.exists(apps_dir):
            os.makedirs(apps_dir)
            print(f"Directorul '{apps_dir}' a fost creat.")

        for file_name in os.listdir(apps_dir):
            if file_name.endswith('.exe'):
                app_full_path = os.path.join(apps_dir, file_name)
                if os.path.isfile(app_full_path):
                    try:
                        self.applications[file_name] = {
                            'name': file_name,
                            'path': app_full_path,
                            'version': os.path.getmtime(app_full_path)
                        }
                        print(f"Aplicația încărcată: {file_name}")
                    except Exception as e:
                        print(f"Eroare la încărcarea metadatelor pentru {file_name}: {e}")

    def start(self):
        """
        Pornește serverul și începe să asculte conexiuni de la clienți.
        Creează un thread nou pentru fiecare client conectat.
        """
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.port))
        server_socket.listen(5)
        print(f"Server pornit pe {self.host}:{self.port}. Apăsați Ctrl+C pentru a opri.")

        try:
            while True:
                client_socket, address = server_socket.accept()
                print(f"Nouă conexiune de la {address}")
                client_thread = threading.Thread(target=self.handle_client, args=(client_socket, address))
                client_thread.daemon = True  # Permite închiderea serverului chiar dacă thread-urile rulează
                client_thread.start()
        except KeyboardInterrupt:
            print("Serverul se oprește...")
        except Exception as e:
            print(f"Eroare la acceptarea conexiunii: {e}")
        finally:
            server_socket.close()
            print("Socket-ul serverului a fost închis.")

    def _send_json_response(self, client_socket, data_dict):
        """ Trimite un răspuns JSON către client. """
        try:
            client_socket.sendall(json.dumps(data_dict).encode('utf-8'))
        except Exception as e:
            # Adresa s-ar putea să nu fie disponibilă aici dacă client_socket e deja închis
            print(f"Eroare la trimiterea răspunsului JSON: {e}")

    def handle_client(self, client_socket, address):
        """
        Gestionează comunicarea cu un client conectat.
        Procesează cererile clientului pentru listarea și descărcarea aplicațiilor.
        """
        print(f"Manipulare client {address}")
        try:
            while True:
                client_socket.settimeout(300.0)  # 5 minute timeout pentru o cerere de la client
                data_received_bytes = client_socket.recv(4096)
                if not data_received_bytes:
                    print(f"Clientul {address} s-a deconectat (nu s-au primit date).")
                    break

                client_socket.settimeout(None)  # Resetăm timeout-ul după primirea cererii

                try:
                    request_str = data_received_bytes.decode('utf-8')
                    request = json.loads(request_str)
                    print(f"Cerere JSON primită de la {address}: {request_str}")
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    print(
                        f"Eroare la decodarea cererii JSON de la {address}: {e}. Date primite: {data_received_bytes!r}")
                    self._send_json_response(client_socket, {'status': 'error', 'message': 'Cerere JSON invalidă.'})
                    break  # Protocol invalid, închidem conexiunea cu acest client

                command = request.get('command')
                print(f"Comanda '{command}' primită de la {address}.")

                if command == 'list_apps':
                    self._send_json_response(client_socket,
                                             {'status': 'success', 'apps': list(self.applications.keys())})

                elif command == 'download_app':
                    app_name = request.get('app_name')
                    if app_name and app_name in self.applications:
                        app_info = self.applications[app_name]
                        app_file_path = app_info['path']
                        try:
                            with open(app_file_path, 'rb') as f:
                                app_data = f.read()

                            metadata = {
                                'status': 'success',
                                'app_name': app_name,
                                'version': app_info['version'],
                                'size': len(app_data)
                            }
                            self._send_json_response(client_socket, metadata)

                            client_socket.settimeout(60.0)
                            ack = client_socket.recv(1024).decode('utf-8', errors='ignore')
                            client_socket.settimeout(None)

                            if ack == 'READY':
                                print(
                                    f"Clientul {address} este gata. Se trimite fișierul {app_name} ({len(app_data)} bytes)...")
                                client_socket.sendall(app_data)
                                print(f"Fișierul {app_name} a fost trimis complet către {address}.")

                                client_socket.settimeout(60.0)
                                final_ack = client_socket.recv(1024).decode('utf-8', errors='ignore')
                                client_socket.settimeout(None)

                                if final_ack == 'DONE':
                                    with self.lock:
                                        self.client_apps.setdefault(address, set()).add(app_name)
                                    print(f"Transferul pentru {app_name} către {address} confirmat de client.")
                                else:
                                    print(
                                        f"Confirmare finală ('{final_ack}') invalidă de la {address} pentru {app_name}.")
                            else:
                                print(f"Clientul {address} nu a trimis 'READY' pentru {app_name}. Răspuns: '{ack}'.")
                        except FileNotFoundError:
                            print(f"Eroare pe server: Fișierul {app_file_path} nu a fost găsit pentru {app_name}.")
                            self._send_json_response(client_socket, {'status': 'error',
                                                                     'message': f'Fișierul {app_name} nu există pe server.'})
                        except socket.timeout as ste:
                            print(f"Server: Timeout în timpul transferului cu {address} pentru {app_name}: {ste}")
                            # Nu mai trimitem răspuns, clientul probabil a renunțat
                        except Exception as e_file_transfer:
                            print(f"Server: Eroare la transferul {app_name} către {address}: {e_file_transfer}")
                            try:
                                self._send_json_response(client_socket, {'status': 'error',
                                                                         'message': f'Eroare server la transferul {app_name}: {str(e_file_transfer)}'})
                            except Exception as e_send_err_nested:
                                print(
                                    f"Server: Nu s-a putut trimite mesajul de eroare post-transfer către {address}: {e_send_err_nested}")
                    else:
                        self._send_json_response(client_socket, {'status': 'error', 'message': f'Aplicația ',
                                                                 {app_name}:' nu a fost găsită pe server.'})
                else:
                    self._send_json_response(client_socket, {'status': 'error', 'message': f'Comanda ',
                                                             {command}:' este necunoscută sau invalidă.'})

        except socket.timeout:
            print(f"Timeout în așteptarea datelor de la clientul {address}. Se închide conexiunea.")
        except ConnectionResetError:
            print(f"Clientul {address} a resetat conexiunea.")
        except Exception as e_client_loop:
            print(f"Eroare neașteptată în handle_client pentru {address}: {e_client_loop}")
        finally:
            print(f"Se închide conexiunea cu clientul {address}.")
            client_socket.close()
            # Nu ștergem client_apps aici pentru a păstra evidența aplicațiilor descărcate anterior
            # Aceasta ar putea fi gestionată diferit dacă e necesar (ex: timeout de inactivitate pentru client_apps)

    def update_application(self, app_name, new_version_path):
        """
        Actualizează o aplicație existentă cu o nouă versiune (funcție de administrare server).
        """
        if app_name in self.applications:
            try:
                if not os.path.exists(new_version_path) or not os.path.isfile(new_version_path):
                    print(
                        f"Eroare la actualizare: Noua cale pentru {app_name} ({new_version_path}) nu există sau nu este un fișier.")
                    return

                old_path = self.applications[app_name]['path']
                os.replace(new_version_path, old_path)
                self.applications[app_name]['version'] = os.path.getmtime(old_path)
                print(
                    f"Aplicația {app_name} a fost actualizată pe server la versiunea {self.applications[app_name]['version']}.")

                # Logica de notificare a clienților este complexă și nu face parte din acest protocol simplu.
                print(f"Notă: Clienții existenți NU sunt notificați automat despre această actualizare.")

            except Exception as e:
                print(f"Eroare la actualizarea aplicației {app_name} pe server: {e}")
        else:
            print(f"Eroare la actualizare: Aplicația {app_name} nu există pe server.")


if __name__ == '__main__':
    server = ApplicationServer()
    server.start()  # start() acum gestionează KeyboardInterrupt și închiderea socket-ului principal
    print("Server oprit complet.")