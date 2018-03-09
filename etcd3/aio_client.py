"""
asynchronous client
"""

import json
import ssl

import aiohttp

from .baseclient import BaseClient, BaseModelizedStreamResponse
from .errors import Etcd3APIError, Etcd3StreamError, Etcd3Exception


class ModelizedStreamResponse(BaseModelizedStreamResponse):
    """
    Model of a stream response
    """

    def __init__(self, method, resp, decode=True):
        """
        :param resp: aiohttp.ClientResponse
        """
        self.resp = resp
        self.decode = decode
        self.method = method
        self.resp_iter = ResponseIter(resp)

    def close(self):
        """
        close the stream
        """
        return self.resp.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.close()

    async def __aiter__(self):
        return self

    async def __anext__(self):
        data = await self.resp_iter.next()
        data = json.loads(str(data, encoding='utf-8'))
        if data.get('error'):
            # {"error":{"grpc_code":14,"http_code":503,"message":"rpc error: code = Unavailable desc = transport is closing","http_status":"Service Unavailable"}}
            err = data.get('error')
            raise Etcd3APIError(err.get('message'), code=err.get('code'), status=err.get('http_code'))
        if 'result' in data:
            data = data.get('result', {})  # the real data is put under the key: 'result'
        return AioClient._modelizeResponseData(self.method, data, decode=self.decode)


class ResponseIter():
    """
    yield response content by every json object
    we don't yield by line, because the content of etcd's gRPC-JSON-Gateway stream response
    does not have a delimiter between each object by default. (only one line)

    https://github.com/grpc-ecosystem/grpc-gateway/pull/497/files

    :param resp: aiohttp.ClientResponse
    :return: dict
    """

    def __init__(self, resp):
        self.resp = resp
        self.buf = []
        self.bracket_flag = 0

    async def __aiter__(self):
        return self

    async def next(self):
        while True:
            c = await self.resp.content.read(1)
            if not c:
                if self.buf:
                    raise Etcd3StreamError("Stream decode error", self.buf, self.resp)
                raise StopAsyncIteration
            self.buf.append(c)
            if c == b'{':
                self.bracket_flag += 1
            elif c == b'}':
                self.bracket_flag -= 1
            if self.bracket_flag == 0:
                s = b''.join(self.buf)
                self.buf = []
                return s
            elif self.bracket_flag < 0:
                raise Etcd3StreamError("Stream decode error", self.buf, self.resp)

    __anext__ = next


class AioClient(BaseClient):
    def __init__(self, host='localhost', port=2379, protocol='http',
                 ca_cert=None, cert_key=None, cert_cert=None,
                 timeout=None, headers=None, user_agent=None, pool_size=30,
                 user=None, password=None, token=None):
        super(AioClient, self).__init__(host=host, port=port, protocol=protocol,
                                        ca_cert=ca_cert, cert_key=cert_key, cert_cert=cert_cert,
                                        timeout=timeout, headers=headers, user_agent=user_agent, pool_size=pool_size,
                                        user=user, password=password, token=token)
        if self.cert:
            ssl_context = ssl.SSLContext()
            ssl_context.load_cert_chain(*self.cert)
            connector = aiohttp.TCPConnector(limit=pool_size, ssl=ssl_context)
        else:
            connector = aiohttp.TCPConnector(limit=pool_size)
        self.session = aiohttp.ClientSession(connector=connector)

    async def close(self):
        """
        close all connections in connection pool
        """
        await self.session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    @classmethod
    def _modelizeStreamResponse(cls, method, resp, decode=True):
        return ModelizedStreamResponse(method, resp, decode)

    async def _get(self, url, **kwargs):
        r"""
        Sends a GET request. Returns :class:`Response` object.

        :param url: URL for the new :class:`Request` object.
        :param \*\*kwargs: Optional arguments that ``request`` takes.
        :rtype: aiohttp.ClientResponse
        """
        return await self.session.get(url, **kwargs)

    async def _post(self, url, data=None, json=None, **kwargs):
        r"""
        Sends a POST request. Returns :class:`Response` object.

        :param url: URL for the new :class:`Request` object.
        :param data: (optional) Dictionary, bytes, or file-like object to send in the body of the :class:`Request`.
        :param json: (optional) json to send in the body of the :class:`Request`.
        :param \*\*kwargs: Optional arguments that ``request`` takes.
        :rtype: aiohttp.ClientResponse
        """
        return await self.session.post(url, data=data, json=json, **kwargs)

    @staticmethod
    async def _raise_for_status(resp):
        status = resp.status
        if status < 400:
            return
        try:
            data = await resp.json()
        except Exception:
            error = resp.content
            code = 2
        else:
            error = data.get('error')
            code = data.get('code')
        raise Etcd3APIError(error, code, status, resp)

    async def call_rpc(self, method, data=None, stream=False, encode=True, raw=False, **kwargs):
        """
        call ETCDv3 RPC and return response object

        :type method: str
        :param method: the rpc method, which is a path of RESTful API
        :type data: dict
        :param data: request payload to be post to ETCD's gRPC-JSON-Gateway default: {}
        :type stream: bool
        :param stream: whether return a stream response object, default: False
        :type encode: bool
        :param encode: whether encode the data before post, default: True
        :param kwargs: additional params to pass to the http request, like headers, timeout etc.
        :return: Etcd3RPCResponseModel or Etcd3StreamingResponse
        """
        data = data or {}
        kwargs.setdefault('timeout', self.timeout)
        kwargs.setdefault('headers', {}).setdefault('user_agent', self.user_agent)
        kwargs.setdefault('headers', {}).update(self.headers)
        if encode:
            data = self._encodeRPCRequest(method, data)
        resp = await self._post(self._url(method), json=data or {}, **kwargs)
        await self._raise_for_status(resp)
        if raw:
            return resp
        if stream:
            try:
                return self._modelizeStreamResponse(method, resp)
            except Etcd3Exception:
                resp.close()
        return self._modelizeResponseData(method, await resp.json())