"""Create the initial administrator; passwords are read without echo."""
import argparse
import getpass
from .db import connection, init_db, now, password_hash


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--username', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    password = getpass.getpass('Contraseña (mínimo 12 caracteres): ')
    if len(password) < 12:
        parser.error('La contraseña requiere al menos 12 caracteres')
    if password != getpass.getpass('Repite la contraseña: '):
        parser.error('Las contraseñas no coinciden')
    init_db()
    with connection() as db:
        db.execute('INSERT INTO users(username,name,password,role,project,specialty,created_at) VALUES(?,?,?,?,?,?,?)',
                   (args.username, args.name, password_hash(password), 'admin', args.project, 'Coordinación', now()))
    print('Administrador creado. Puedes iniciar sesión en la aplicación.')


if __name__ == '__main__':
    main()
