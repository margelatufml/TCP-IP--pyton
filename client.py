import socket
import json
import os
import threading
import time
import subprocess
from pathlib import Path


class ApplicationClient:
    """
    Clasa client care se conectează la serverul de aplicații.
    Gestionează descărcarea, rularea și actualizarea aplicațiilor.
    Păstrează copii locale ale aplicațiilor descărcate și ține evidența versiunilor.
    """

    def __init__(self, host='localhost', port=5000):
        """
        Inițializează clientul cu setările de conectare la server.
        Creează structurile necesare pentru urmărirea aplicațiilor descărcate și rulante.
        """
        self.host = host
        self.port = port
        self.downloaded_apps = {}  # Dicționar pentru aplicațiile descărcate și versiunile lor
        self.running_apps = {}  # Dicționar pentru aplicațiile în execuție (cele pornite cu Popen)
        self.lock = threading.Lock()  # Blocare pentru operații thread-safe

        if not os.path.exists('downloads'):
            os.makedirs('downloads')

    def connect(self):
        """
        Stabilește conexiunea cu serverul.
        """
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.host, self.port))
        print(f"Conectat la serverul {self.host}:{self.port}")

    def receive_json(self):
        """
        Primește și decodifică un mesaj JSON de la server.
        Acumulează date până când un JSON valid poate fi parsat.
        """
        buffer = b''
        try:
            self.socket.settimeout(10.0)  # Timeout pentru a nu aștepta la nesfârșit
            while True:
                chunk = self.socket.recv(4096)  # Citim în bucăți mai mari
                if not chunk:
                    # Conexiune închisă înainte de a primi un JSON complet
                    if buffer:
                        raise json.JSONDecodeError("Conexiune închisă prematur, buffer incomplet.",
                                                   buffer.decode('utf-8', errors='replace'), 0)
                    return None  # Nimic de parsat

                buffer += chunk
                try:
                    # Încercăm să decodăm și să parsăm întregul buffer acumulat
                    # Presupunem că serverul trimite mesaje JSON distincte și complete
                    json_obj = json.loads(buffer.decode('utf-8'))
                    return json_obj
                except UnicodeDecodeError as ude:
                    # Dacă apar erori de decodare în mijlocul unui mesaj, e problematic
                    # Poate indica date corupte sau un protocol nealiniat
                    print(f"Avertisment: Eroare de decodare Unicode în buffer: {ude}. Buffer curent: {buffer!r}")
                    # Continuăm să acumulăm, sperând că se va rezolva sau JSONDecodeError va prinde
                except json.JSONDecodeError:
                    # JSON-ul nu este încă complet, continuăm să acumulăm
                    # Această excepție va fi ridicată în mod repetat până când buffer-ul conține un JSON valid
                    pass
        except socket.timeout:
            print("Eroare: Timeout la primirea răspunsului JSON de la server.")
            return {"status": "error", "message": "Timeout la citirea răspunsului JSON."}
        except Exception as e:
            print(f"Eroare critică la primirea JSON: {e}. Buffer: {buffer.decode('utf-8', errors='replace')}")
            return {"status": "error", "message": f"Eroare la citirea răspunsului: {e}"}
        finally:
            self.socket.settimeout(None)  # Resetează la blocant
        return None  # Dacă bucla se termină fără a returna (ex. conexiune închisă și buffer gol)

    def get_applications_list(self):
        """
        Solicită și primește lista de aplicații disponibile de la server.
        """
        request = {'command': 'list_apps'}
        try:
            self.socket.sendall(json.dumps(request).encode('utf-8'))
            response = self.receive_json()
            if response and response.get('status') == 'success':
                return response.get('apps', [])
            else:
                error_msg = response.get('message') if response else "Răspuns invalid sau gol de la server."
                print(f"Eroare la obținerea listei de aplicații: {error_msg}")
                return []
        except Exception as e:
            print(f"Eroare la trimiterea cererii pentru lista de aplicații: {e}")
            return []

    def download_application(self, app_name):
        """
        Descarcă o aplicație de la server.
        """
        request = {'command': 'download_app', 'app_name': app_name}
        try:
            self.socket.sendall(json.dumps(request).encode('utf-8'))
            metadata = self.receive_json()

            if not metadata or metadata.get('status') == 'error':
                error_msg = metadata.get('message') if metadata else "Răspuns invalid pentru metadate."
                print(f"Eroare la primirea metadatelor pentru {app_name}: {error_msg}")
                return False

            file_size = metadata['size']
            version = metadata['version']

            print(f"Începe descărcarea {app_name} (Dimensiune: {file_size} bytes)")
            self.socket.sendall('READY'.encode('utf-8'))

            app_path = os.path.join('downloads', app_name)
            temp_path = app_path + '.tmp'

            bytes_received = 0
            with open(temp_path, 'wb') as f:
                last_progress_display_time = time.time()
                self.socket.settimeout(60.0)  # Timeout mai mare pentru descărcarea datelor

                while bytes_received < file_size:
                    try:
                        remaining_bytes = file_size - bytes_received
                        chunk_size_to_receive = min(8192, remaining_bytes)
                        chunk = self.socket.recv(chunk_size_to_receive)
                        if not chunk:
                            if bytes_received < file_size:
                                raise EOFError("Conexiunea s-a închis prematur în timpul descărcării.")
                            break

                        f.write(chunk)
                        bytes_received += len(chunk)

                        current_time = time.time()
                        if current_time - last_progress_display_time >= 0.5:
                            progress = (bytes_received / file_size) * 100
                            print(f"\rProgres descărcare: {progress:.1f}% ({bytes_received}/{file_size} bytes)      ",
                                  end='', flush=True)
                            last_progress_display_time = current_time
                    except socket.timeout:
                        print(
                            f"\nTimeout la descărcarea datelor pentru {app_name}. {bytes_received}/{file_size} bytes primiți.")
                        raise

            print(f"\rProgres descărcare: 100.0% ({file_size}/{file_size} bytes)      ", flush=True)
            print("\nDescărcare finalizată teoretic!")

            # Verificare finală a dimensiunii
            if os.path.getsize(temp_path) != file_size:
                raise ValueError(
                    f"Dimensiunea fișierului descărcat ({os.path.getsize(temp_path)}) nu corespunde cu cea așteptată ({file_size}). Fișier posibil corupt.")

            self.socket.sendall('DONE'.encode('utf-8'))

            if os.path.exists(app_path):
                os.remove(app_path)
            os.rename(temp_path, app_path)

            self.downloaded_apps[app_name] = version
            print(f"Aplicația {app_name} (versiune {version}) a fost descărcată și verificată cu succes.")
            return True

        except (EOFError, ValueError, socket.timeout) as e:
            print(f"\nEroare specifică la descărcarea {app_name}: {e}")
        except Exception as e:
            print(f"\nEroare generală la descărcarea {app_name}: {e}")
        finally:
            self.socket.settimeout(None)
            if 'temp_path' in locals() and os.path.exists(temp_path):
                # Șterge fișierul temporar doar dacă eroarea nu e ValueError legată de mărime,
                # pentru a permite inspecția manuală dacă e cazul.
                if not isinstance(e, ValueError) or "Dimensiunea fișierului descărcat" not in str(e):
                    try:
                        os.remove(temp_path)
                    except OSError as ose:
                        print(f"Nu s-a putut șterge fișierul temporar {temp_path}: {ose}")
        return False

    def run_application(self, app_name):
        """
        Rulează o aplicație descărcată.
        """
        if app_name not in self.downloaded_apps:
            print(f"Aplicația {app_name} nu a fost descărcată sau informațiile lipsesc.")
            return False

        app_path_relative = os.path.join('downloads', app_name)
        app_path_absolute = os.path.abspath(app_path_relative)

        print(f"Se încearcă pornirea aplicației: {app_path_absolute}")

        if not os.path.exists(app_path_absolute):
            print(f"Eroare critică: Fișierul {app_path_absolute} nu există deși verificarea inițială a trecut.")
            return False

        try:
            if os.name == 'nt':  # Specific pentru Windows
                try:
                    print(f"Windows: Se încearcă lansarea {app_name} cu os.startfile...")
                    os.startfile(app_path_absolute)
                    print(
                        f"Aplicația {app_name} a fost transmisă sistemului pentru lansare (via os.startfile). Verifică dacă a pornit.")
                    # os.startfile este asincron și nu returnează un proces pe care să-l urmărim direct aici
                    # Eliminăm din running_apps dacă a fost cumva adăugat de o tentativă anterioară
                    if app_name in self.running_apps:
                        del self.running_apps[app_name]
                    return True
                except OSError as e:
                    if hasattr(e, 'winerror') and e.winerror == 193:
                        print(f"EROARE (WinError 193) la lansarea '{app_name}' cu os.startfile: {e.strerror}")
                        print(f"  Calea completă: {app_path_absolute}")
                        print("  Această eroare indică de obicei una dintre următoarele:")
                        print("    1. Fișierul descărcat este CORUPT sau INCOMPLET.")
                        print("       Verificați dimensiunea fișierului în directorul 'downloads'.")
                        print("       Încercați să ștergeți fișierul și să-l descărcați din nou.")
                        print(
                            "    2. Aplicația necesită DREPTURI DE ADMINISTRATOR pentru a rula (valabil pentru instalatori).")
                        print("       ÎNCERCAȚI SĂ RULAȚI ACEST SCRIPT (client.py) CA ADMINISTRATOR.")
                        print("    3. Există o NEPOTRIVIRE DE ARHITECTURĂ (deși ați menționat că sunt 64-bit).")
                        print("       Verificați arhitectura Python (32/64 bit) vs arhitectura aplicației.")
                        print("    4. Software-ul ANTIVIRUS blochează execuția.")
                        print(
                            "  PAS DE DEPANARE IMPORTANT: Încercați să rulați '{app_name}' MANUAL din directorul 'downloads'.")
                    else:
                        print(f"Eroare OSError neașteptată la lansarea {app_name} cu os.startfile: {e}")
                    return False
            else:  # Pentru alte sisteme de operare (Linux, macOS)
                print(f"Non-Windows: Se încearcă lansarea {app_name} cu subprocess.Popen...")
                process = subprocess.Popen([app_path_absolute])
                self.running_apps[app_name] = process  # Permite oprirea ulterioară
                print(f"Aplicația {app_name} a fost pornită (PID: {process.pid}).")
                return True

        except FileNotFoundError:
            print(
                f"Eroare critică: Fișierul executabil {app_path_absolute} nu a fost găsit deși verificarea inițială a trecut.")
        except PermissionError:
            print(
                f"Eroare: Permisiuni insuficiente pentru a rula {app_path_absolute}. Încercați să rulați clientul ca administrator.")
        except Exception as e:
            print(f"Eroare generală la pornirea {app_name}: {e}")
            if isinstance(e, OSError) and hasattr(e, 'winerror') and e.winerror == 193:
                print(
                    "  Sfat suplimentar pentru WinError 193: Dacă rularea manuală funcționează, problema poate fi legată de mediul de execuție al scriptului Python.")
        return False

    def update_application(self, app_name, new_version_data):
        """
        Actualizează o aplicație descărcată cu o nouă versiune.
        """
        app_path = os.path.join('downloads', app_name)
        temp_path = app_path + '.new'

        try:
            with open(temp_path, 'wb') as f:
                f.write(bytes.fromhex(new_version_data))

            # Verifică dacă aplicația rulează (dacă a fost pornită cu Popen)
            process_to_terminate = self.running_apps.get(app_name)
            if process_to_terminate and process_to_terminate.poll() is None:  # Verifică dacă procesul încă rulează
                print(f"Încerc să opresc aplicația {app_name} (PID: {process_to_terminate.pid}) pentru actualizare...")
                process_to_terminate.terminate()
                try:
                    process_to_terminate.wait(timeout=5)
                    print(f"Aplicația {app_name} a fost oprită.")
                except subprocess.TimeoutExpired:
                    print(f"Aplicația {app_name} nu s-a oprit la timp. Se încearcă forțarea (kill)...")
                    process_to_terminate.kill()
                    process_to_terminate.wait(timeout=5)
                    print(f"Aplicația {app_name} a fost forțat oprită.")
                del self.running_apps[app_name]
            else:
                if app_name in self.downloaded_apps:  # Doar dacă aplicația a fost descărcată prin client
                    print(
                        f"Aplicația {app_name} nu rulează (sau a fost pornită prin os.startfile și nu poate fi oprită automat). Actualizarea va suprascrie fișierul.")

            os.replace(temp_path, app_path)
            print(f"Aplicația {app_name} a fost actualizată cu succes pe disc.")
            # Ar trebui să actualizăm și versiunea în self.downloaded_apps dacă serverul trimite noua versiune
            # Aici presupunem că 'new_version_data' este doar conținutul, nu și metadate noi de versiune.
            # Pentru o actualizare completă a versiunii, serverul ar trebui să retrimită metadatele.
            return True
        except Exception as e:
            print(f"Eroare la actualizarea {app_name}: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return False


def main():
    """
    Funcția principală care rulează aplicația client.
    """
    client = ApplicationClient()
    try:
        client.connect()

        while True:
            print("\n--- Meniu Client ---")
            print("1. Listează aplicațiile disponibile pe server")
            print("2. Descarcă o aplicație")
            print("3. Rulează o aplicație descărcată")
            print("4. Ieșire")

            choice = input("Introduceți opțiunea (1-4): ")

            if choice == '1':
                apps = client.get_applications_list()
                if apps:
                    print("\nAplicații disponibile pe server:")
                    for app in apps:
                        print(f"- {app}")
                else:
                    print("Nicio aplicație disponibilă sau eroare la listare.")

            elif choice == '2':
                app_name = input("Introduceți numele exact al aplicației de descărcat (ex: MyApp.exe): ")
                if app_name:
                    client.download_application(app_name)
                else:
                    print("Numele aplicației nu poate fi gol.")

            elif choice == '3':
                app_name = input("Introduceți numele exact al aplicației descărcate de rulat (ex: MyApp.exe): ")
                if app_name:
                    client.run_application(app_name)
                else:
                    print("Numele aplicației nu poate fi gol.")

            elif choice == '4':
                print("Se închide clientul...")
                break
            else:
                print("Opțiune invalidă. Vă rugăm introduceți un număr între 1 și 4.")

    except ConnectionRefusedError:
        print(
            "EROARE CRITICĂ: Conexiunea la server a fost refuzată. Verificați dacă serverul rulează și este accesibil.")
    except KeyboardInterrupt:
        print("\nClient închis de utilizator.")
    except Exception as e:
        print(f"Eroare neașteptată în client: {e}")
    finally:
        if hasattr(client, 'socket') and client.socket:
            try:
                client.socket.close()
            except Exception as e_sock:
                print(f"Eroare la închiderea socket-ului: {e_sock}")
        print("Clientul s-a oprit.")


if __name__ == '__main__':
    main()