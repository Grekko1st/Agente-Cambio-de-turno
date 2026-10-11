# Relevo · Agente de cambio de turno

Primera versión funcional de una bitácora para construcción. Durante un período de ocho días, cada persona registra avances, trabajos en curso, pendientes, planificación, posibles adicionales e interferencias. Al finalizar el octavo día se prepara una entrega para el contraturno. El coordinador consulta todas las especialidades de su obra durante el período, por defecto en una vista diaria.

## Qué incluye

- Cuentas con invitaciones de un solo uso y roles de administración, coordinación y supervisor/contraturno.
- Aislamiento por obra y especialidad. Supervisores de la misma especialidad comparten su bitácora; coordinación consulta todas las especialidades de su obra.
- Reportes de texto, grabación en navegador y carga de audio. El audio original se conserva incluso sin transcripción.
- SQLite persistente, historial de correcciones, autor y fecha de cada registro. Cambiar un pendiente deja un evento de auditoría.
- Aclaraciones del coordinador al autor del reporte, con horas y personas opcionales. La clasificación de un adicional o interferencia la confirma coordinación.
- Pendientes que continúan entre períodos y personas. Nunca se cierran por una inferencia de la IA.
- Cierre automático del bloque 8×6, con fecha inicial, horario y zona horaria configurables. Entregas con revisión del autor y recepción por otra persona de la misma especialidad.
- Respaldo local diario de la base de datos y los audios vinculados mientras la aplicación está ejecutándose.
- Adaptadores opcionales para Telegram, transcripción y consultas por IA con referencias a reportes.

## Instalación y arranque

Requiere Python 3.12. Desde la raíz del repositorio:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m app.admin --username administrador --name 'Administrador de obra' --project 'Mi obra'
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

El comando de administración solicita una contraseña sin mostrarla. No hay cuentas ni contraseñas predeterminadas. Crea el administrador una sola vez por obra; puede generar invitaciones desde **Mi turno y accesos**. Para otra obra, crea un administrador con otro usuario y otro nombre de obra.

La aplicación se sirve en el puerto indicado. No hay un despliegue público incluido. La grabación por navegador requiere HTTPS o un contexto local seguro; la carga de archivos es otra opción.

1. Inicia sesión como administrador y crea invitaciones para supervisores, contraturnos y coordinadores.
2. Cada persona registra su cuenta con la invitación y configura su primer período.
3. Registra notas en **Bitácora diaria**, agregando pendientes cuando corresponda.
4. Coordinación filtra los reportes y solicita aclaraciones. El autor ve la consulta junto al reporte y responde allí.
5. Al terminar el octavo día, el autor revisa su entrega. El contraturno de esa especialidad confirma la recepción.
6. Para el período siguiente se propone la fecha inicial + 14 días y se conserva horario/zona. La creación de cada nuevo período es explícita; no se inventan asignaciones ni calendarios de contraturnos.

Los cierres se revisan cada 30 segundos. Si la aplicación estuvo detenida, genera los cierres vencidos al arrancar. Para cerrar puntualmente debe permanecer ejecutándose. Una entrega con audios pendientes no puede marcarse revisada. Las correcciones invalidan una revisión anterior; una entrega recibida conserva su contenido, y las aclaraciones posteriores permanecen consultables en la bitácora.

## Datos y respaldos

Por defecto, `data/bitacora.sqlite3` contiene los registros y `data/audio/` los audios. Ambos están excluidos de Git. Puedes configurar `BITACORA_DATA_DIR` con una ruta absoluta en un volumen persistente. La aplicación debe tener permiso de escritura.

Se crea un archivo diario en `data/backups/`, usando el día UTC, o en `BITACORA_BACKUP_DIR` si está configurado. Los respaldos incluyen un snapshot consistente de SQLite y los audios referenciados. No se eliminan automáticamente. Un respaldo local no cubre la pérdida del volumen: para un uso operativo, conserva una copia en otro almacenamiento y define su retención.

Respaldo manual:

```bash
.venv/bin/python -m app.backup /ruta/de/respaldo/bitacora.tar.gz
```

Para restaurar, detén la aplicación y extrae un respaldo propio en un directorio nuevo; conserva `bitacora.sqlite3` y `audio/`. Las rutas de audio del respaldo son absolutas: restaura en la ruta original o actualízalas con un procedimiento de migración antes de arrancar. Esta primera versión no incluye una herramienta de restauración ni migraciones entre versiones de esquema. No sobrescribas una base existente.

## Voz e IA

Configura `TRANSCRIPTION_API_KEY` de forma segura en las variables del proceso o en los secretos del entorno, con destino `api.openai.com`. No escribas claves en Git ni en el chat. La clave necesita acceso a transcripción y a consultas de texto.

- `TRANSCRIPTION_MODEL`: por defecto `gpt-4o-mini-transcribe`.
- `EXTRACTION_MODEL`: por defecto `gpt-4.1-mini`.

El proveedor recibe el audio para transcribirlo y el texto para proponer categorías y pendientes. Las consultas de coordinación envían únicamente los reportes de su obra incluidos en sus filtros, con un máximo de 100 reportes por consulta. Coordina el uso de ese proveedor con tu organización antes de enviar reportes reales.

Sin clave, texto, pendientes, permisos y entregas funcionan; los audios se guardan con transcripción pendiente. Si falla la IA, no se pierde el audio ni una transcripción ya obtenida. El autor puede reintentar o corregir manualmente la nota, conservando el historial. Las sugerencias de la IA necesitan revisión; no constituyen decisiones de costos ni aprobaciones de adicionales.

## Telegram

Configura `TELEGRAM_BOT_TOKEN` con destino `api.telegram.org`. Crea un bot propio y usa una sola instancia de esta aplicación para consultarlo. El adaptador usa polling; un bot con webhook activo debe cambiarse a polling antes de usarlo aquí.

1. En la aplicación, genera un código temporal desde **Vincular Telegram**.
2. Envía el comando generado en un chat privado con tu bot. El código vence en 15 minutos y se usa una sola vez.
3. Envía notas de texto o voz durante tu período activo. Se guardan en el turno de la cuenta vinculada.
4. Usa `/bitacora` para ver las últimas cinco notas y `/pendientes` para ver hasta diez tareas abiertas desde el propio bot. Revisa el detalle completo, las correcciones y las aclaraciones en la aplicación web.

No procesa mensajes de grupos. Un identificador de Telegram no concede acceso por sí solo. Los reintentos de una misma actualización no duplican reportes. Las notas de texto enviadas al bot se conservan tal cual; su categorización inicial es «Realizado» y no se extraen pendientes por IA de esas notas de texto. Para registrar planificación o tareas explícitas usa el formulario web; el audio sí pasa por extracción cuando la IA está configurada.

### Prueba sin acceso a la interfaz web

Desde la terminal del proyecto puedes preparar una cuenta de prueba con permisos de supervisor, una obra separada y un período activo:

```bash
.venv/bin/python -m app.telegram_setup --demo
```

El comando imprime un código `/vincular` de un solo uso que vence en 15 minutos. Envíalo en el chat privado de tu bot. La cuenta de prueba usa Telegram; no tiene una contraseña web compartida. Repetir el comando reutiliza la cuenta y el período activo y revoca el código anterior. Genera el código cuando el bot ya esté conectado, para evitar que venza durante su configuración.

Para vincular una cuenta existente, usa `--username nombre_de_usuario` en lugar de `--demo`. Esta opción no crea ni modifica sus períodos. Una vez vinculado, envía una nota de texto o voz y comprueba su presencia con `/bitacora`. Sin clave de IA, un audio se conserva como pendiente de transcripción.

## Validación

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
```

Las pruebas cubren autenticación, permisos entre obras y especialidades, invitaciones, consultas y respuestas, historial, cierre al octavo día, recepción, continuidad de pendientes, almacenamiento de audio, recuperación de errores, respaldos y adaptadores externos con respuestas simuladas. Los servicios reales requieren credenciales y una prueba posterior.

## Alcance de esta primera versión

El flujo web y la persistencia están implementados. WhatsApp, notificaciones automáticas de nuevas consultas, asignación explícita de parejas de contraturnos, despliegue público y almacenamiento remoto de respaldos quedan para una siguiente etapa. Por ahora otra persona de la misma especialidad puede recibir una entrega revisada; no se impone una pareja fija.

Para exponerla públicamente configura HTTPS, `COOKIE_SECURE=1`, un volumen persistente y supervisión del proceso; añade límites de solicitudes y cargas en el proxy. Usa una sola instancia de Uvicorn para evitar duplicar polling y procesos de mantenimiento. No es un despliegue de producción ya configurado.
