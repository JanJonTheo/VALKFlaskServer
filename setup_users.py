import sqlite3
import bcrypt
import sys
import os

# === KONFIGURATION ===
DB_PATH = "instance/bgs_data.db"  # Pfad zur SQLite-Datenbank
USERNAME = "admin"       # Benutzername, dessen Passwort aktualisiert werden soll
NEW_PASSWORD = "passAdmin"  # Neues Passwort im Klartext

# === FUNKTION: Passwort erstellen oder aktualisieren ===
def setup_admin_user(db_path, username, plain_password):
    if not os.path.exists(db_path):
        print(f"❌ Datenbank nicht gefunden: {db_path}")
        return

    # Passwort hashen
    hashed_password = bcrypt.hashpw(plain_password.encode(), bcrypt.gensalt()).decode()

    # Verbindung herstellen
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Prüfen, ob der Benutzer bereits existiert
        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        user_exists = cursor.fetchone()

        if user_exists:
            # Benutzer existiert -> Passwort aktualisieren
            cursor.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hashed_password, username)
            )
            print(f"✅ Passwort für Benutzer '{username}' wurde erfolgreich aktualisiert.")
        else:
            # Benutzer existiert nicht -> Neu anlegen
            cursor.execute(
                "INSERT INTO users (username, password_hash, is_admin, active) VALUES (?, ?, ?, ?)",
                (username, hashed_password, True, True)
            )
            print(f"✅ Benutzer '{username}' wurde erstellt und Passwort gesetzt.")

        conn.commit()
        conn.close()

    except Exception as e:
        print(f"❌ Fehler beim Einrichten des Admin-Benutzers: {e}")

# === Hauptfunktion aufrufen ===
if __name__ == "__main__":
    setup_admin_user(DB_PATH, USERNAME, NEW_PASSWORD)