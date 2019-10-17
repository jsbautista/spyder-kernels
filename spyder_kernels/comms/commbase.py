# -*- coding: utf-8 -*-
#
# Copyright © Spyder Project Contributors
# Licensed under the terms of the MIT License
# (see spyder/__init__.py for details)

"""
Class that handles communications between Spyder kernel and frontend.

Comms transmit data in a list of buffers, and in a json-able dictionnary.
Here, we only support a buffer list with a single element.

The messages exchanged have the following msg_dict:

    ```
    msg_dict = {
        'spyder_msg_type': spyder_msg_type,
        'content': content,
    }
    ```

The buffer is generated by cloudpickle using `PICKLE_PROTOCOL = 2`.

To simplify the usage of messaging, we use a higher level function calling
mechanism:
    - The `remote_call` method returns a RemoteCallHandler object
    - By calling an attribute of this object, the call is sent to the other
      side of the comm.
    - If the `_wait_reply` is implemented, remote_call can be called with
      `blocking=True`, which will wait for a reply sent by the other side.

The messages exchanged are:
    - Function call (spyder_msg_type = 'remote_call'):
        - The content is a dictionnary {
            'call_name': The name of the function to be called,
            'call_id': uuid to match the request to a potential reply,
            'settings': A dictionnary of settings,
            }
        - The buffer encodes a dictionnary {
            'call_args': The function args,
            'call_kwargs': The function kwargs,
            }
    - If the 'settings' has `'blocking' =  True`, a reply is sent.
      (spyder_msg_type = 'remote_call_reply'):
        - The buffer contains the return value of the function.
        - The 'content' is a dict with: {
                'is_error': a boolean indicating if the return value is an
                            exception to be raised.
                'call_id': The uuid from above,
                'call_name': The function name (mostly for debugging)
                }
"""
from __future__ import print_function

import cloudpickle
import pickle
import logging
import sys
import uuid
import traceback

from spyder_kernels.py3compat import PY2, PY3


logger = logging.getLogger(__name__)

# To be able to get and set variables between Python 2 and 3
DEFAULT_PICKLE_PROTOCOL = 2


class CommError(RuntimeError):
    pass


class CommsErrorWrapper():
    def __init__(self, call_name, call_id):
        self.call_name = call_name
        self.call_id = call_id
        self.etype, self.error, tb = sys.exc_info()
        self.tb = traceback.extract_tb(tb)

    def raise_error(self):
        """
        Raise the error while adding informations on the callback.
        """
        # Add the traceback in the error, so it can be handled upstream
        raise self.etype(self)

    def format_error(self):
        """
        Format the error recieved from the other side and returns a list of
        strings.
        """
        lines = (['Exception in comms call {}:\n'.format(self.call_name)]
                 + traceback.format_list(self.tb)
                 + traceback.format_exception_only(self.etype, self.error))
        return lines

    def print_error(self, file=None):
        """
        Print the error to file or to sys.stderr if file is None.
        """
        if file is None:
            file = sys.stderr
        for line in self.format_error():
            print(line, file=file)

    def __str__(self):
        """Get string representation"""
        return str(self.error)

    def __repr__(self):
        """Get string representation"""
        return repr(self.error)


# Replace sys.excepthook to handle CommsErrorWrapper
sys_excepthook = sys.excepthook


def comm_excepthook(type, value, tb):
    if len(value.args) == 1 and isinstance(value.args[0], CommsErrorWrapper):
        traceback.print_tb(tb)
        value.args[0].print_error()
        return
    sys_excepthook(type, value, tb)


sys.excepthook = comm_excepthook


class CommBase(object):
    """
    Class with the necessary attributes and methods to handle
    communications between a kernel and a frontend.
    Subclasses must open a comm and register it with `self._register_comm`.
    """

    def __init__(self):
        super(CommBase, self).__init__()
        self.calling_comm_id = None
        self._comms = {}
        # Handlers
        self._message_handlers = {}
        self._remote_call_handlers = {}
        # Lists of reply numbers
        self._reply_inbox = {}
        self._reply_waitlist = {}

        self._register_message_handler(
            'remote_call', self._handle_remote_call)
        self._register_message_handler(
            'remote_call_reply', self._handle_remote_call_reply)

        # Dummy functions for testing and to trigger side effects such as
        # an interruption or waiting for a reply.
        def pong_back():
            self.remote_call(self.calling_comm_id).pong()

        self.register_call_handler('ping', pong_back)
        self.register_call_handler('pong', lambda: None)
        self.register_call_handler('_set_pickle_protocol',
                                   self._set_pickle_protocol)

    def get_comm_id_list(self, comm_id=None):
        """Get a list of comms id"""
        if comm_id is None:
            id_list = list(self._comms.keys())
        else:
            id_list = [comm_id]
        return id_list

    def close(self, comm_id=None):
        """Close the comm and notify the other side."""
        id_list = self.get_comm_id_list(comm_id)

        for comm_id in id_list:
            self._comms[comm_id]['comm'].close()
            del self._comms[comm_id]

    def is_open(self, comm_id=None):
        """Check to see if the comm is open."""
        if comm_id is None:
            return len(self._comms) > 0
        return comm_id in self._comms

    def is_ready(self, comm_id=None):
        """
        Check to see if the other side replied.

        The check is made with _set_pickle_protocol as this is the first call
        made. If comm_id is not specified, check all comms.
        """
        id_list = self.get_comm_id_list(comm_id)
        if len(id_list) == 0:
            return False
        return all([self._comms[cid]['status'] == 'ready' for cid in id_list])

    def register_call_handler(self, call_name, handler):
        """
        Register a remote call handler.

        Parameters
        ----------
        call_name : str
            The name of the called function.
        handler : callback
            A function to handle the request, or `None` to unregister
            `call_name`.
        """
        if not handler:
            self._remote_call_handlers.pop(call_name, None)
            return

        self._remote_call_handlers[call_name] = handler

    def remote_call(self, comm_id=None, callback=None, **settings):
        """Get a handler for remote calls."""
        return RemoteCallFactory(self, comm_id, callback, **settings)

    # ---- Private -----
    def _send_message(self, spyder_msg_type, content=None, data=None,
                      comm_id=None):
        """
        Publish custom messages to the other side.

        Parameters
        ----------
        spyder_msg_type: str
            The spyder message type
        content: dict
            The (JSONable) content of the message
        data: any
            Any object that is serializable by cloudpickle (should be most
            things). Will arrive as cloudpickled bytes in `.buffers[0]`.
        comm_id: int
            the comm to send to. If None sends to all comms.
        """
        if not self.is_open(comm_id):
            raise CommError("The comm is not connected.")
        id_list = self.get_comm_id_list(comm_id)
        for comm_id in id_list:
            msg_dict = {
                'spyder_msg_type': spyder_msg_type,
                'content': content,
                'pickle_protocol': self._comms[comm_id]['pickle_protocol'],
                'python_version': sys.version,
                }
            buffers = [cloudpickle.dumps(
                data, protocol=self._comms[comm_id]['pickle_protocol'])]
            self._comms[comm_id]['comm'].send(msg_dict, buffers=buffers)

    def _set_pickle_protocol(self, protocol):
        """Set the pickle protocol used to send data."""
        protocol = min(protocol, pickle.HIGHEST_PROTOCOL)
        self._comms[self.calling_comm_id]['pickle_protocol'] = protocol
        self._comms[self.calling_comm_id]['status'] = 'ready'

    @property
    def _comm_name(self):
        """
        Get the name used for the underlying comms.
        """
        return 'spyder_api'

    def _register_message_handler(self, message_id, handler):
        """
        Register a message handler.

        Parameters
        ----------
        message_id : str
            The identifier for the message
        handler : callback
            A function to handle the message. This is called with 3 arguments:
                - msg_dict: A dictionary with message information.
                - buffer: The data transmitted in the buffer
                - load_exception: Exception from buffer unpickling
            Pass None to unregister the message_id
        """
        if handler is None:
            self._message_handlers.pop(message_id, None)
            return

        self._message_handlers[message_id] = handler

    def _register_comm(self, comm):
        """
        Open a new comm to the kernel.
        """
        comm.on_msg(self._comm_message)
        comm.on_close(self._comm_close)
        self._comms[comm.comm_id] = {
            'comm': comm,
            'pickle_protocol': DEFAULT_PICKLE_PROTOCOL,
            'status': 'opening',
            }

    def _comm_close(self, msg):
        """Close comm."""
        comm_id = msg['content']['comm_id']
        del self._comms[comm_id]

    def _comm_message(self, msg):
        """
        Handle internal spyder messages.
        """
        self.calling_comm_id = msg['content']['comm_id']
        # Load the buffer. Only one is supported.
        try:
            if PY3:
                # https://docs.python.org/3/library/pickle.html#pickle.loads
                # Using encoding='latin1' is required for unpickling
                # NumPy arrays and instances of datetime, date and time
                # pickled by Python 2.
                buffer = cloudpickle.loads(msg['buffers'][0],
                                           encoding='latin-1')
            else:
                buffer = cloudpickle.loads(msg['buffers'][0])
            load_exception = None
        except Exception as e:
            load_exception = e
            buffer = None

        # Get message dict
        msg_dict = msg['content']['data']

        spyder_msg_type = msg_dict['spyder_msg_type']

        if spyder_msg_type in self._message_handlers:
            self._message_handlers[spyder_msg_type](
                msg_dict, buffer, load_exception)
        else:
            logger.debug("No such spyder message type: %s" % spyder_msg_type)

    def _handle_remote_call(self, msg, buffer, load_exception):
        """Handle a remote call."""
        msg_dict = msg['content']
        if load_exception:
            logger.debug(
                "Exception in cloudpickle.loads : %s" % str(load_exception))
            return
        try:
            return_value = self._remote_callback(
                    msg_dict['call_name'],
                    buffer['call_args'],
                    buffer['call_kwargs'])
            self._set_call_return_value(msg_dict, return_value)
        except Exception:
            exc_infos = CommsErrorWrapper(
                msg_dict['call_name'], msg_dict['call_id'])
            self._set_call_return_value(msg_dict, exc_infos, is_error=True)

    def _remote_callback(self, call_name, call_args, call_kwargs):
        """Call the callback function for the remote call."""
        if call_name in self._remote_call_handlers:
            return self._remote_call_handlers[call_name](
                *call_args, **call_kwargs)

        raise CommError("No such spyder call type: %s" % call_name)

    def _set_call_return_value(self, call_dict, data, is_error=False):
        """
        A remote call has just been processed.

        This will reply if settings['blocking'] == True
        """
        settings = call_dict['settings']
        send_reply = 'send_reply' in settings and settings['send_reply']

        if not send_reply and not is_error:
            # Nothing to send back
            return
        content = {
            'is_error': is_error,
            'call_id': call_dict['call_id'],
            'call_name': call_dict['call_name']
        }

        self._send_message('remote_call_reply', content=content, data=data,
                           comm_id=self.calling_comm_id)

    def _register_call(self, call_dict, callback=None):
        """
        Register the call so the reply can be properly treated.
        """
        settings = call_dict['settings']
        blocking = 'blocking' in settings and settings['blocking']
        call_id = call_dict['call_id']
        if blocking or callback is not None:
            self._reply_waitlist[call_id] = blocking, callback

    def _get_call_return_value(self, call_dict, call_data, comm_id):
        """
        Send a remote call and return the reply.

        If settings['blocking'] == True, this will wait for a reply and return
        the replied value.
        """
        self._send_message(
            'remote_call', content=call_dict, data=call_data,
            comm_id=comm_id)

        settings = call_dict['settings']

        blocking = 'blocking' in settings and settings['blocking']

        if not blocking:
            return

        call_id = call_dict['call_id']
        call_name = call_dict['call_name']

        # Wait for the blocking call
        if 'timeout' in settings:
            timeout = settings['timeout']
        else:
            timeout = 3  # Seconds

        self._wait_reply(call_id, call_name, timeout)

        reply = self._reply_inbox.pop(call_id)

        if reply['is_error']:
            return self._sync_error(reply['value'])

        return reply['value']

    def _wait_reply(self, call_id, call_name, timeout):
        """
        Wait for the other side reply.
        """
        raise NotImplementedError

    def _handle_remote_call_reply(self, msg_dict, buffer, load_exception):
        """
        A blocking call received a reply.
        """
        content = msg_dict['content']
        call_id = content['call_id']
        call_name = content['call_name']
        if load_exception:
            buffer = load_exception, []
            is_error = True
        else:
            is_error = content['is_error']

        # Unexpected reply
        if call_id not in self._reply_waitlist:
            if is_error:
                return self._async_error(buffer)
            else:
                logger.debug('Got an unexpected reply {}, id:{}'.format(
                    call_name, call_id))
            return

        blocking, callback = self._reply_waitlist.pop(call_id)

        # Async error
        if is_error and not blocking:
            return self._async_error(buffer)

        # Callback
        if callback is not None and not is_error:
            callback(buffer)

        # Blocking inbox
        if blocking:
            self._reply_inbox[call_id] = {
                    'is_error': is_error,
                    'value': buffer,
                    'content': content
                    }

    def _async_error(self, error_wrapper):
        """
        Handle an error that was raised on the other side asyncronously.
        """
        error_wrapper.print_error()

    def _sync_error(self, error_wrapper):
        """
        Handle an error that was raised on the other side syncronously.
        """
        error_wrapper.raise_error()


class RemoteCallFactory(object):
    """Class to create `RemoteCall`s."""

    def __init__(self, comms_wrapper, comm_id, callback, **settings):
        # Avoid setting attributes
        super(RemoteCallFactory, self).__setattr__(
            '_comms_wrapper', comms_wrapper)
        super(RemoteCallFactory, self).__setattr__('_comm_id', comm_id)
        super(RemoteCallFactory, self).__setattr__('_callback', callback)
        super(RemoteCallFactory, self).__setattr__('_settings', settings)

    def __getattr__(self, name):
        """Get a call for a function named 'name'."""
        return RemoteCall(name, self._comms_wrapper, self._comm_id,
                          self._callback, self._settings)

    def __setattr__(self, name, value):
        """Set an attribute to the other side."""
        raise NotImplementedError


class RemoteCall():
    """Class to call the other side of the comms like a function."""

    def __init__(self, name, comms_wrapper, comm_id, callback, settings):
        self._name = name
        self._comms_wrapper = comms_wrapper
        self._comm_id = comm_id
        self._settings = settings
        self._callback = callback

    def __call__(self, *args, **kwargs):
        """
        Transmit the call to the other side of the tunnel.

        The args and kwargs have to be picklable.
        """
        blocking = 'blocking' in self._settings and self._settings['blocking']
        self._settings['send_reply'] = blocking or self._callback is not None

        call_id = uuid.uuid4().hex
        call_dict = {
            'call_name': self._name,
            'call_id': call_id,
            'settings': self._settings,
            }
        call_data = {
            'call_args': args,
            'call_kwargs': kwargs,
            }

        if not self._comms_wrapper.is_open(self._comm_id):
            # Only an error if the call is blocking.
            if blocking:
                raise CommError("The comm is not connected.")
            logger.debug("Call to unconnected comm: %s" % self._name)
            return
        self._comms_wrapper._register_call(call_dict, self._callback)
        return self._comms_wrapper._get_call_return_value(
            call_dict, call_data, self._comm_id)
