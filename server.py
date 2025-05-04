from multiprocessing.forkserver import connect_to_new_process

import socket
# THESE COMMENTS WERE MADE BY Margelatu Andrei Cristian to Explain the code (no, it's not AI :))) )

# this line will create a TCP/IP socket
socket=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
# bind the socket to the specific port for the server
server_adress=('localhost',10000)
print('starting up on %s port %s'% server_adress)
socket.bind(server_adress)

#listen for inbound connections (we will wait for multiple connections)
socket.listen(1)

while True:
    #wait for a connection
    print('waiting for a connection')
    connection, client_adress=socket.accept()

    try:
        print('Connectio from', client_adress)
        # receive the data into bites
        while True:
            data =connection.recv(1600)
            print('received "%s"' % data)
            if data:
                print('sending data to the client (same of an ACK frame)')
                msg = str(data)
                msg = msg + 'Plus extra from the server'
                msg = bytes(msg, 'utf-8')
                connection.sendall(msg)
            else:
                print('no more data from', client_adress)
                break
    finally:
        connection.close()