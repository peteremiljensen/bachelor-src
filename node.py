import threading
import asyncio
import janus
import time
import json

from .events import Events
from .network import Network
from .chain import *
from .loaf import Loaf
from .block import Block
from .validator import Validator

from .common import *

from queue import Empty as SyncQueueEmpty

#   _   _           _
#  | \ | |         | |
#  |  \| | ___   __| | ___
#  | . ` |/ _ \ / _` |/ _ \
#  | |\  | (_) | (_| |  __/
#  \_| \_/\___/ \__,_|\___|
#

class Node():
    def __init__(self, port):
        """ Node class constructor
        """
        self._network = Network(port)

        self._chain = Chain()
        self._loaf_pool = {}
        self._loaves_lock = threading.RLock()
        self._mined_loaves = {}

        self._events_thread = threading.Thread(target=self._start_events_thread,
                                               daemon=True)
        self._events_thread.start()
        time.sleep(0.5) ## TODO: Shall be fixed to a smarter solution
        self._worker_thread = threading.Thread(target=self._worker_thread,
                                               daemon=True)

    def start(self):
        """ Starts the node by creating a worker thread and a network thread """
        self._network.start()
        self._worker_thread.start()

        def new_connection_callback(websocket):
            self._get_hashes(websocket)
        Events.Instance().register_callback(EVENTS_TYPE.CONNECTION_READY,
                                            new_connection_callback)

    def attach_loaf_validator(self, function):
        Validator.Instance().attach_loaf_validator(function)

    def attach_block_validator(self, function):
        Validator.Instance().attach_block_validator(function)

    def attach_branching_check(self, function):
        Validator.Instance().attach_branching_check(function)

    def attach_branching(self, function):
        Validator.Instance().attach_branching(function)

    def connect_node(self, ip, port=9000):
        """ Connects to another node through its IP address """
        self._network.connect_node(ip, port)

    def add_loaf(self, loaf):
        with self._loaves_lock:
            if not loaf.validate():
                print(fail('Loaf could not validate'))
                return False
            if loaf.get_hash() in self._loaf_pool:
                return False
            if loaf.get_hash() in self._mined_loaves.keys():
                print(warning('Loaf has already been mined'))
                return False
            self._loaf_pool[loaf.get_hash()] = loaf
            return True

    def add_block(self, block):
        height = block.get_height()
        with self._loaves_lock:
            for loaf in block.get_loaves():
                try:
                    del self._loaf_pool[loaf.get_hash()]
                except KeyError:
                    pass
                self._mined_loaves[loaf.get_hash()] = height
        return self._chain.add_block(block)

    def get_loaves(self):
        loaves = []
        with self._loaves_lock:
            loaves_total = 0
            loaves_hash = []
            loaf_pool_keys = list(self._loaf_pool.keys())
            loaves_total = min(1000, len(loaf_pool_keys))
            loaves_hash = loaf_pool_keys[:loaves_total]
            for h in loaves_hash:
                loaves.append(self._loaf_pool[h])

        return loaves

    def get_chain(self):
        return self._chain

    def replace_chain(self, chain):
        with self._loaves_lock:
            # Put loaves from existing chain to loaf_pool
            for block in self._chain.get_blocks(0, self._chain.get_length()):
                for loaf in block.get_loaves():
                    try:
                        del self._mined_loaves[loaf.get_hash()]
                    except KeyError:
                        pass
                    self._loaf_pool[loaf.get_hash()] = loaf
            # Put loaves from new chain into mined_loaves
            for block in chain.get_blocks(0, chain.get_length()):
                for loaf in block.get_loaves():
                    try:
                        del self._loaf_pool[loaf.get_hash()]
                    except KeyError:
                        pass
                    self._mined_loaves[loaf.get_hash()] = loaf
            self._chain.replace(chain.get_blocks(0, chain.get_length()))

    def broadcast_loaf(self, loaf):
        """ Validates a loaf. If it is validated, it puts the loaves hash in
            the loaf pool and broadcasts it to all connected nodes
        """
        self._network.broadcast(
            self._json({'type': 'request',
                        'function': FUNCTIONS.BROADCAST_LOAF,
                        'loaf': loaf}))

    def broadcast_block(self, block):
        self._network.broadcast(
            self._json({'type': 'request',
                        'function': FUNCTIONS.BROADCAST_BLOCK,
                        'block': block}))

    def _get_hashes(self, websocket):
        """ Requests the length of the blockchain from a node """
        self._network.send(websocket, self._json(
            {'type': 'request',
             'function': FUNCTIONS.GET_HASHES}))

    def _get_blocks(self, websocket, offset, length):
        self._network.send(websocket, self._json(
            {'type': 'request',
             'function': FUNCTIONS.GET_BLOCKS,
             'offset': offset,
             'length': length}))

    def _start_events_thread(self):
        """ Starts an event thread that runs untill completion """
        try:
            Events.Instance().start()
        except:
            print(fail('fatal error regarding events'))
            raise

    def _worker_thread(self):
        """ Starts the worker thread, which listens for requests from other
            nodes in the network.
        """
        while True:
            for websocket in list(self._network.get_queues().keys()):
                try:
                    raw_data = self._network.recv_nowait(websocket)
                    message = json.loads(raw_data.decode('utf-8'))

                    if message['type'] == 'error':
                        if 'description' in message:
                            desc = message['description']
                        else:
                            desc = 'No description'
                        Events.Instance().notify(EVENTS_TYPE.RECEIVED_ERROR,
                                                 desc)
                        print(fail('Error received: ' + desc))
                    elif message['type'] != 'request' and \
                         message['type'] != 'response':
                        self._network.send(
                            websocket, self._json({'type': 'error',
                                                   'description': \
                                                   'type is not supported'}))
                    elif message['function'] == FUNCTIONS.GET_HASHES:
                        self._handle_get_hashes(message, websocket)
                    elif message['function'] == FUNCTIONS.GET_BLOCKS:
                        self._handle_get_blocks(message, websocket)
                    elif message['function'] == FUNCTIONS.BROADCAST_LOAF:
                        self._handle_broadcast_loaf(message)
                    elif message['function'] == FUNCTIONS.BROADCAST_BLOCK:
                        self._handle_broadcast_block(message, websocket)
                    else:
                        self._network.send(
                            websocket, self._json({'type': 'error',
                                                   'description':
                                                   'Unsupported function'}))

                except (AttributeError, KeyError):
                    response = self._json({'type': 'error',
                                           'description': 'Attribute or ' + \
                                           'KeyError. Consider adding "raise"'})
                    self._network.send(websocket, response)
                except SyncQueueEmpty:
                    pass
                except:
                    print(fail("fatal error"))
                    raise
            time.sleep(0.05)

    def _handle_get_hashes(self, message, websocket):
        if message['type'] == 'request':
            hashes = self._chain.get_hashes()
            response = self._json({'type': 'response',
                                   'function': FUNCTIONS.GET_HASHES,
                                   'hashes': hashes})
            self._network.send(websocket, response)
        elif message['type'] == 'response':
            remote_hashes = message['hashes']
            local_hashes = self._chain.get_hashes()
            offset = 0
            for i in range(min(len(remote_hashes), len(local_hashes))):
                if remote_hashes[i] == local_hashes[i]:
                    offset += 1
                else:
                    break
            if offset < len(remote_hashes):
                length = len(remote_hashes) - offset
                self._get_blocks(websocket, offset, length)

    def _handle_get_blocks(self, message, websocket):
        if message['type'] == 'request':
            offset = message['offset']
            length = message['length']
            blocks = self._chain.get_blocks(offset, length)
            response = self._json({'type': 'response',
                                   'function': FUNCTIONS.GET_BLOCKS,
                                   'blocks': blocks})
            self._network.send(websocket, response)
        elif message['type'] == 'response':
            blocks = []
            for block in message['blocks']:
                blocks.append(Block.create_block_from_dict(block))
            chain_length = self._chain.get_length()
            if len(blocks) > 0 and chain_length > 0 and \
               blocks[0].get_height() <= chain_length:
                local_chain = Chain()
                remote_chain = Chain()
                mutual = self._chain.get_blocks(0, blocks[0].get_height())
                remote_chain.replace(mutual)
                # Make sure received blocks are valid
                for block in blocks:
                    if not remote_chain.add_block(block):
                        print(warning("blocks received are not valid"))
                        return
                # Test if blocks are directly appendable
                top_block = self._chain.get_block(chain_length-1)
                if blocks[0].get_height() == chain_length and \
                   blocks[0].get_previous_block_hash() == top_block.get_hash():
                    for block in blocks:
                        self.add_block(block)
                # Branching method has to be consulted
                else:
                    local_chain.replace(
                        self._chain.get_blocks(0, self._chain.get_length()))
                    chain = Validator.Instance().branching(
                        local_chain,
                        remote_chain)
                    self.replace_chain(chain)
                top_block = self._chain.get_block(self._chain.get_length()-1)
                self.broadcast_block(top_block)
                Events.Instance().notify(EVENTS_TYPE.BLOCKS_ADDED, None)
                print(info('Chain has been replaced'))
            else:
                print(warning("blocks received is invalid"))

    def _handle_broadcast_loaf(self, message):
        """ Receives and validates a loaf. If loaf is not validated,
            an error message is displayed. If loaf is validated, it checks
            whether loaf already exists in the local loaf pool. If it does not
            exist in the loaf pool, it adds the loaf to the loaf pool and
            broadcasts the loaf to all connected nodes.
        """
        loaf = Loaf.create_loaf_from_dict(message['loaf'])
        with self._loaves_lock:
            if self.add_loaf(loaf):
                print(info('Received loaf and forwarding it'))
                Events.Instance().notify(EVENTS_TYPE.RECEIVED_LOAF, loaf)
                self.broadcast_loaf(loaf)

    def _handle_broadcast_block(self, message, websocket):
        block = Block.create_block_from_dict(message['block'])
        block_height = block.get_height()
        chain_length = self._chain.get_length()

        if block_height != chain_length:
            if block_height >= chain_length or block.get_hash() != \
              self._chain.get_block(block_height).get_hash():
                self._get_hashes(websocket)

        elif self.add_block(block):
            print(info('Block succesfully added'))
            Events.Instance().notify(EVENTS_TYPE.RECEIVED_BLOCK, block)
            self.broadcast_block(block)

        else:
            print(fail('block could not be added'))

    @staticmethod
    def _json(dictio):
        """ Serializes object to a JSON formatted string, encodes to utf-8
            and returns
        """
        return json.dumps(dictio, cls=BlockEncoder,
            separators=(',', ':')).encode('utf-8')
