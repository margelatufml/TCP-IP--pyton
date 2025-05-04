import socket

sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)

# connect the socket to the port where the server is listening
server_adress=('localhost',10000)
print(' connecting to %s port %s' % server_adress)
sock.connect(server_adress)

try:
    #We will send data to the server
    message='Acesta este un mesaj, just repeat it'
    print('sending %s' % message)
    sock.sendall(bytes(message,'utf-8'))

    #look for a response back
    amount_received=0
    amount_expected = len(message)
    #this is kinda the checksum method

    while amount_received<amount_expected:
        data=sock.recv(1600)
        amount_received += len(data)
        print('received "%s"' % data)
finally:
    print('Closing socket')
    sock.close()