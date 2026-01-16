const WebSocket = require('ws');
const http = require('http');
const fs = require('fs');
const path = require('path');

const server = http.createServer((req, res) => {
    if(req.url === '/') {
        const file = path.join(__dirname, 'index.html');
        res.writeHead(200, {'Content-Type': 'text/html'});
        res.end(fs.readFileSync(file));
    } else {
        res.writeHead(404);
        res.end();
    }
});

const wss = new WebSocket.Server({ server });

wss.on('connection', ws => {
    console.log('Client connected');
});

function broadcast(data) {
    const msg = JSON.stringify(data);
    wss.clients.forEach(client => {
        if(client.readyState === WebSocket.OPEN) client.send(msg);
    });
}

server.listen(8080, () => console.log('UI server running at http://localhost:8080'));

// Экспортируем broadcast, чтобы Python мог писать в WebSocket через tiny ws server
module.exports = { broadcast };
