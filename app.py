"""
Control de Gastos · Flask + SQLite
----------------------------------
- Acceso con usuario y contraseña (usuario: admin, clave: 123).
- Registro de gastos: fecha, descripción y monto.
- Reporte por rango de fechas, con totales por mes y exportación a CSV.

Ejecutar:
    pip install -r requirements.txt
    python app.py        -> http://127.0.0.1:5057
"""

import csv
import io
import os
import secrets
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    DATABASE=os.path.join(BASE, "gastos.db"),
    SESSION_COOKIE_NAME="gastos_session",   # cookie propia: no se mezcla con otras apps en 127.0.0.1
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS usuario (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario       TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gasto (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha       TEXT    NOT NULL,                 -- AAAA-MM-DD
    descripcion TEXT    NOT NULL,
    monto       REAL    NOT NULL CHECK (monto > 0),
    creado_en   TEXT    NOT NULL
);
"""


# ----------------------------------------------------------------------
# Base de datos
# ----------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def cerrar_db(_=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(app.config["DATABASE"])
    db.executescript(SCHEMA)
    if db.execute("SELECT COUNT(*) FROM usuario").fetchone()[0] == 0:
        # La clave se guarda como hash, nunca en texto plano
        db.execute("INSERT INTO usuario (usuario, password_hash) VALUES (?, ?)",
                   ("admin", generate_password_hash("123")))
    db.commit()
    db.close()


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------

def soles(valor):
    return f"S/ {valor:,.2f}"


app.jinja_env.filters["soles"] = soles


@app.template_filter("fecha")
def formato_fecha(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def verificar_csrf():
    if request.method == "POST":
        token = request.form.get("_csrf", "")
        if not token or not secrets.compare_digest(token, session.get("_csrf", "")):
            abort(400)


def login_requerido(vista):
    @wraps(vista)
    def envoltura(*args, **kwargs):
        if "usuario" not in session:
            return redirect(url_for("login"))
        return vista(*args, **kwargs)
    return envoltura


def validar_gasto(form):
    """Devuelve (datos, errores)."""
    errores = []
    fecha = form.get("fecha", "").strip()
    descripcion = " ".join(form.get("descripcion", "").split())[:120]
    monto_txt = form.get("monto", "").strip().replace(",", ".")
    try:
        f = date.fromisoformat(fecha)
        if f > date.today():
            errores.append("La fecha no puede ser futura.")
    except ValueError:
        errores.append("Ingrese una fecha válida.")
    if len(descripcion) < 3:
        errores.append("La descripción debe tener al menos 3 caracteres.")
    monto = None
    try:
        monto = Decimal(monto_txt)
        if monto <= 0 or monto > Decimal("1000000"):
            errores.append("El monto debe ser mayor que 0 y menor que 1 000 000.")
        elif monto.as_tuple().exponent < -2:
            errores.append("El monto admite como máximo 2 decimales.")
    except InvalidOperation:
        errores.append("Ingrese un monto numérico, por ejemplo 25.50.")
    datos = {"fecha": fecha, "descripcion": descripcion, "monto": monto_txt}
    return datos, errores, (float(monto) if monto is not None and not errores else None)


# ----------------------------------------------------------------------
# Acceso
# ----------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def login():
    if "usuario" in session:
        return redirect(url_for("gastos"))
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        clave = request.form.get("clave", "")
        fila = get_db().execute("SELECT * FROM usuario WHERE usuario = ?", (usuario,)).fetchone()
        if fila and check_password_hash(fila["password_hash"], clave):
            session.clear()
            session["usuario"] = fila["usuario"]
            return redirect(url_for("gastos"))
        flash("Usuario o clave incorrectos.", "error")
        return render_template("login.html", usuario=usuario), 401
    return render_template("login.html", usuario="")


@app.route("/salir", methods=["POST"])
def salir():
    session.clear()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("login"))


# ----------------------------------------------------------------------
# Gastos
# ----------------------------------------------------------------------

@app.route("/gastos", methods=["GET", "POST"])
@login_requerido
def gastos():
    db = get_db()
    datos, errores = {"fecha": date.today().isoformat(), "descripcion": "", "monto": ""}, []
    if request.method == "POST":
        datos, errores, monto = validar_gasto(request.form)
        if not errores:
            db.execute("INSERT INTO gasto (fecha, descripcion, monto, creado_en) VALUES (?, ?, ?, ?)",
                       (datos["fecha"], datos["descripcion"], monto, datetime.now().isoformat(sep=" ", timespec="seconds")))
            db.commit()
            flash(f"Gasto registrado: {datos['descripcion']} por {soles(monto)}.", "exito")
            return redirect(url_for("gastos"))
    filas = db.execute("SELECT * FROM gasto ORDER BY fecha DESC, id DESC").fetchall()
    hoy = date.today()
    mes_actual = db.execute("SELECT COALESCE(SUM(monto), 0) FROM gasto WHERE substr(fecha, 1, 7) = ?",
                            (hoy.strftime("%Y-%m"),)).fetchone()[0]
    resumen = {
        "total": sum(f["monto"] for f in filas),
        "cantidad": len(filas),
        "mes": mes_actual,
        "nombre_mes": f"{MESES[hoy.month - 1]} {hoy.year}",
        "hoy": hoy.isoformat(),
    }
    return render_template("gastos.html", filas=filas, datos=datos, errores=errores, resumen=resumen), (400 if errores else 200)


@app.route("/gastos/<int:id_gasto>/eliminar", methods=["POST"])
@login_requerido
def eliminar(id_gasto):
    db = get_db()
    fila = db.execute("SELECT * FROM gasto WHERE id = ?", (id_gasto,)).fetchone()
    if fila is None:
        abort(404)
    db.execute("DELETE FROM gasto WHERE id = ?", (id_gasto,))
    db.commit()
    flash(f"Gasto eliminado: {fila['descripcion']}.", "info")
    return redirect(url_for("gastos"))


# ----------------------------------------------------------------------
# Reporte
# ----------------------------------------------------------------------

def consulta_reporte():
    desde = request.args.get("desde", "")
    hasta = request.args.get("hasta", "")
    sql, params = "SELECT * FROM gasto WHERE 1 = 1", []
    for valor, cond in ((desde, " AND fecha >= ?"), (hasta, " AND fecha <= ?")):
        try:
            date.fromisoformat(valor)
            sql += cond
            params.append(valor)
        except ValueError:
            pass
    filas = get_db().execute(sql + " ORDER BY fecha, id", params).fetchall()
    return filas, desde, hasta


@app.route("/reporte")
@login_requerido
def reporte():
    filas, desde, hasta = consulta_reporte()
    total = sum(f["monto"] for f in filas)
    por_mes = {}
    for f in filas:
        clave = f["fecha"][:7]
        por_mes[clave] = por_mes.get(clave, 0) + f["monto"]
    meses = [{"nombre": f"{MESES[int(k[5:]) - 1].capitalize()} {k[:4]}", "total": v,
              "porcentaje": (v / total * 100) if total else 0} for k, v in sorted(por_mes.items())]
    mayor = max(filas, key=lambda f: f["monto"]) if filas else None
    return render_template("reporte.html", filas=filas, total=total, meses=meses, mayor=mayor,
                           promedio=(total / len(filas)) if filas else 0, desde=desde, hasta=hasta,
                           generado=datetime.now().strftime("%d/%m/%Y %H:%M"))


@app.route("/reporte.csv")
@login_requerido
def reporte_csv():
    filas, _, _ = consulta_reporte()
    salida = io.StringIO()
    w = csv.writer(salida, delimiter=";")
    w.writerow(["Fecha", "Descripción", "Monto (S/)"])
    for f in filas:
        w.writerow([formato_fecha(f["fecha"]), f["descripcion"], f"{f['monto']:.2f}"])
    w.writerow(["", "TOTAL", f"{sum(f['monto'] for f in filas):.2f}"])
    return Response("﻿" + salida.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=reporte_gastos.csv"})


@app.errorhandler(404)
def no_encontrado(_):
    return render_template("error.html", codigo=404, mensaje="La página que busca no existe."), 404


@app.errorhandler(400)
def solicitud_invalida(_):
    return render_template("error.html", codigo=400, mensaje="El formulario expiró. Vuelva a intentarlo."), 400


init_db()

PUERTO = int(os.environ.get("PORT", 5057))

if __name__ == "__main__":
    print(f"\n  Control de Gastos -> http://127.0.0.1:{PUERTO}   (usuario: admin  clave: 123)\n")
    app.run(host="127.0.0.1", port=PUERTO, debug=True) 