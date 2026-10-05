import json, os, random, sqlite3, threading, time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(os.environ.get('DATA_DIR', HERE), 'data.db')
TTL = int(os.environ.get('RESERVATION_TTL', '900'))  # 15 min por defecto
LOCK = threading.RLock()
FLOW = {'PAGADO': 'EN_PREPARACION', 'EN_PREPARACION': 'DESPACHADO'}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT, emoji TEXT, price REAL, stock INTEGER, reserved INTEGER);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, idem_key TEXT UNIQUE, customer TEXT, status TEXT, payment_status TEXT, total REAL, message TEXT, tracking TEXT, created REAL, expires REAL, dispatched REAL);
CREATE TABLE IF NOT EXISTS order_items(order_id INTEGER, product_id INTEGER, qty INTEGER);
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, order_id INTEGER, type TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, order_id INTEGER, event TEXT, detail TEXT);
'''

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now().strftime('%H:%M:%S')

def audit(c, oid, event, detail=''):
    c.execute('INSERT INTO audit(ts,order_id,event,detail) VALUES(?,?,?,?)', (now(), oid, event, detail))

def seed(c):
    c.execute('DELETE FROM products')
    for r in [(1, 'Zapatillas', '👟', 40, 5, 0), (2, 'Mochila', '🎒', 25, 3, 0), (3, 'Gorra', '🧢', 12, 10, 0)]:
        c.execute('INSERT INTO products VALUES(?,?,?,?,?,?)', r)

def init():
    c = db()
    c.executescript(SCHEMA)
    if not c.execute('SELECT 1 FROM products').fetchone():
        seed(c)
    c.commit()
    c.close()

def items_of(c, oid):
    return c.execute('SELECT oi.product_id,oi.qty,p.name,p.emoji FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=?', (oid,)).fetchall()

def release_items(c, oid):
    for it in items_of(c, oid):
        c.execute('UPDATE products SET reserved=reserved-? WHERE id=?', (it['qty'], it['product_id']))

def reserve_items(c, oid):
    its = items_of(c, oid)
    for it in its:
        p = c.execute('SELECT * FROM products WHERE id=?', (it['product_id'],)).fetchone()
        if p['stock'] - p['reserved'] < it['qty']:
            return False
    for it in its:
        c.execute('UPDATE products SET reserved=reserved+? WHERE id=?', (it['qty'], it['product_id']))
    return True

def expire(c):
    for o in c.execute("SELECT * FROM orders WHERE status='RESERVADO' AND expires<?", (time.time(),)).fetchall():
        release_items(c, o['id'])
        c.execute("UPDATE orders SET status='EXPIRADO', message='La reserva venció sin pago; el stock fue liberado' WHERE id=?", (o['id'],))
        audit(c, o['id'], 'RESERVA_EXPIRADA', 'stock liberado')

def view(c, oid):
    o = dict(c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone())
    o['items'] = [dict(i) for i in items_of(c, oid)]
    o['expires_in'] = max(0, int(o['expires'] - time.time())) if o['status'] == 'RESERVADO' and o['expires'] else None
    return o

def create_order(key, b):
    if not key:
        return 400, {'error': 'Falta el header Idempotency-Key'}
    norm = {}
    try:
        for it in b.get('items') or []:
            q = int(it['qty'])
            if q <= 0:
                return 400, {'error': 'cantidad inválida'}
            norm[int(it['product_id'])] = norm.get(int(it['product_id']), 0) + q
    except Exception:
        return 400, {'error': 'items inválidos'}
    if not norm:
        return 400, {'error': 'el carrito está vacío'}
    with LOCK:
        c = db()
        try:
            expire(c)
            ex = c.execute('SELECT id FROM orders WHERE idem_key=?', (key,)).fetchone()
            if ex:
                audit(c, ex['id'], 'PEDIDO_REINTENTO', 'misma Idempotency-Key: se devuelve el pedido existente')
                c.commit()
                d = view(c, ex['id'])
                d['replay'] = True
                return 200, d
            total = 0
            for pid, q in norm.items():
                p = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
                if not p:
                    return 400, {'error': 'producto inexistente'}
                if p['stock'] - p['reserved'] < q:
                    audit(c, None, 'SIN_STOCK', f"{p['name']} x{q}")
                    c.commit()
                    return 409, {'error': 'Sin stock suficiente de ' + p['name']}
                total += p['price'] * q
            cur = c.execute("INSERT INTO orders(idem_key,customer,status,payment_status,total,message,created,expires) VALUES(?,?,?,?,?,?,?,?)",
                            (key, b.get('customer') or 'Cliente web', 'RESERVADO', 'SIN_PAGO', total, '', time.time(), time.time() + TTL))
            oid = cur.lastrowid
            for pid, q in norm.items():
                c.execute('INSERT INTO order_items VALUES(?,?,?)', (oid, pid, q))
                c.execute('UPDATE products SET reserved=reserved+? WHERE id=?', (q, pid))
            audit(c, oid, 'PEDIDO_CREADO', f'total ${total:.2f}; stock reservado por {TTL // 60} min')
            c.commit()
            return 201, view(c, oid)
        finally:
            c.close()

def webhook(b):
    eid, st = b.get('event_id'), b.get('status')
    try:
        oid = int(b.get('order_id'))
    except Exception:
        return 400, {'error': 'order_id inválido'}
    if not eid or st not in ('approved', 'rejected'):
        return 400, {'error': 'event_id y status (approved|rejected) son obligatorios'}
    with LOCK:
        c = db()
        try:
            expire(c)
            o = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
            if not o:
                return 404, {'error': 'pedido inexistente'}
            if c.execute('INSERT OR IGNORE INTO events(event_id,order_id,type,ts) VALUES(?,?,?,?)', (eid, oid, st, now())).rowcount == 0:
                audit(c, oid, 'WEBHOOK_DUPLICADO', eid + ': evento ya procesado, sin efecto')
                c.commit()
                return 200, {'duplicate': True, 'detail': 'evento ya procesado; sin efecto'}
            s = o['status']
            if st == 'rejected':
                if s == 'RESERVADO':
                    release_items(c, oid)
                if s in ('RESERVADO', 'EXPIRADO'):
                    c.execute("UPDATE orders SET status='CANCELADO', message='Pago rechazado: reserva liberada' WHERE id=?", (oid,))
                audit(c, oid, 'PAGO_RECHAZADO')
            else:
                if s == 'RESERVADO':
                    c.execute("UPDATE orders SET status='PAGADO', payment_status='PAGADO' WHERE id=?", (oid,))
                    audit(c, oid, 'PAGO_APROBADO')
                elif s == 'EXPIRADO':
                    if reserve_items(c, oid):
                        c.execute("UPDATE orders SET status='PAGADO', payment_status='PAGADO', message='Pago recibido tras vencer la reserva; se volvió a reservar el stock' WHERE id=?", (oid,))
                        audit(c, oid, 'PAGO_APROBADO', 'se re-reservó el stock')
                    else:
                        c.execute("UPDATE orders SET status='CANCELADO', payment_status='REEMBOLSADO', message='Reembolsado automáticamente: la reserva venció y ya no hay stock' WHERE id=?", (oid,))
                        audit(c, oid, 'COMPENSACION_REEMBOLSO', 'pago aprobado pero falló la reserva → reembolso + aviso al cliente')
                elif s == 'CANCELADO':
                    c.execute("UPDATE orders SET payment_status='REEMBOLSADO' WHERE id=?", (oid,))
                    audit(c, oid, 'COMPENSACION_REEMBOLSO', 'pago sobre pedido cancelado')
                else:
                    audit(c, oid, 'PAGO_DUPLICADO_IGNORADO', 'el pedido ya estaba pagado')
            c.commit()
            return 200, view(c, oid)
        finally:
            c.close()

def advance(oid):
    with LOCK:
        c = db()
        try:
            o = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
            if not o:
                return 404, {'error': 'pedido inexistente'}
            if o['status'] not in FLOW:
                return 409, {'error': 'no se puede avanzar desde ' + o['status']}
            new = FLOW[o['status']]
            if new == 'DESPACHADO':
                for it in items_of(c, oid):
                    c.execute('UPDATE products SET stock=stock-?, reserved=reserved-? WHERE id=?', (it['qty'], it['qty'], it['product_id']))
                tr = 'EC' + str(random.randint(10 ** 8, 10 ** 9 - 1))
                c.execute("UPDATE orders SET status=?, tracking=?, dispatched=? WHERE id=?", (new, tr, time.time(), oid))
                audit(c, oid, 'DESPACHADO', f'stock descontado; guía {tr}')
                audit(c, oid, 'NOTIFICACION', 'cliente notificado con número de guía')
            else:
                c.execute('UPDATE orders SET status=? WHERE id=?', (new, oid))
                audit(c, oid, new, 'cambio de estado por el operador')
            c.commit()
            return 200, view(c, oid)
        finally:
            c.close()

def force_expire(oid):
    with LOCK:
        c = db()
        try:
            c.execute("UPDATE orders SET expires=? WHERE id=? AND status='RESERVADO'", (time.time() - 1, oid))
            expire(c)
            c.commit()
            return 200, view(c, oid)
        finally:
            c.close()

def metrics():
    c = db()
    O = [dict(x) for x in c.execute('SELECT * FROM orders').fetchall()]
    ev = lambda e: c.execute('SELECT COUNT(*) FROM audit WHERE event=?', (e,)).fetchone()[0]
    out = {
        'pedidos_totales': len(O),
        'pedidos_completados': len([o for o in O if o['status'] == 'DESPACHADO']),
        'cancelaciones': len([o for o in O if o['status'] in ('CANCELADO', 'EXPIRADO')]),
        'errores_de_pago': ev('PAGO_RECHAZADO') + ev('COMPENSACION_REEMBOLSO'),
        'webhooks_duplicados_ignorados': ev('WEBHOOK_DUPLICADO'),
    }
    d = [o['dispatched'] - o['created'] for o in O if o['dispatched']]
    out['tiempo_compra_despacho_s'] = round(sum(d) / len(d), 1) if d else None
    c.close()
    return out

def rows(sql, args=()):
    c = db()
    out = [dict(x) for x in c.execute(sql, args).fetchall()]
    c.close()
    return out

def route(method, path, body, headers):
    p = [x for x in path.split('/') if x][1:]
    if method == 'GET' and p == ['health']:
        return 200, {'ok': True}
    if method == 'GET' and p == ['products']:
        with LOCK:
            c = db(); expire(c); c.commit(); c.close()
        return 200, rows('SELECT * FROM products ORDER BY id')
    if method == 'POST' and p == ['orders']:
        return create_order(headers.get('Idempotency-Key'), body)
    if method == 'GET' and p == ['orders']:
        with LOCK:
            c = db()
            expire(c)
            c.commit()
            out = [view(c, r['id']) for r in c.execute('SELECT id FROM orders ORDER BY id DESC LIMIT 30').fetchall()]
            c.close()
        return 200, out
    if method == 'POST' and p == ['payments', 'webhook']:
        return webhook(body)
    if method == 'POST' and len(p) == 3 and p[0] == 'orders' and p[2] == 'advance':
        return advance(int(p[1]))
    if method == 'POST' and len(p) == 3 and p[0] == 'orders' and p[2] == 'expire':
        return force_expire(int(p[1]))
    if method == 'GET' and p == ['audit']:
        return 200, rows('SELECT * FROM audit ORDER BY id DESC LIMIT 80')
    if method == 'GET' and p == ['metrics']:
        return 200, metrics()
    if method == 'POST' and p == ['reset']:
        with LOCK:
            c = db()
            c.executescript('DELETE FROM orders; DELETE FROM order_items; DELETE FROM events; DELETE FROM audit;')
            seed(c)
            c.commit()
            c.close()
        return 200, {'ok': True}
    return 404, {'error': 'ruta no encontrada'}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def reply(self, code, obj, ctype='application/json'):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def handle_any(self, method):
        if method == 'GET' and self.path in ('/', '/index.html'):
            return self.reply(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html')
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        try:
            code, obj = route(method, self.path.split('?')[0], body, self.headers)
        except Exception as e:
            code, obj = 500, {'error': str(e)}
        self.reply(code, obj)
    def do_GET(self): self.handle_any('GET')
    def do_POST(self): self.handle_any('POST')

if __name__ == '__main__':
    init()
    print('Servidor en http://localhost:8000', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8000), H).serve_forever()
