"""Private pure-compute pipe worker. Never imports the app, Store or HA client."""
import json
import os
import pickle
import struct
import sys

MAX_FRAME = 16 * 1024**2


def read_frame(stream):
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise EOFError('incomplete compute frame')
    length, = struct.unpack('!I', header)
    if length > MAX_FRAME:
        raise ValueError('compute frame exceeds limit')
    data = bytearray()
    while len(data) < length:
        part = stream.read(length-len(data))
        if not part:
            raise EOFError('incomplete compute payload')
        data.extend(part)
    return bytes(data)


def write_frame(stream, value):
    data = pickle.dumps(value, protocol=5)
    if len(data) > MAX_FRAME:
        raise ValueError('compute result exceeds limit')
    stream.write(struct.pack('!I', len(data)))
    stream.write(data)
    stream.flush()
    return len(data)


def compute(operation, value, options):
    if operation == 'json':
        return json.dumps(value, **options)
    if operation == 'roundtrip':
        return json.loads(json.dumps(value))
    if operation == 'fit':
        from binary_state_classifier import BinaryStateClassifier
        classifier = BinaryStateClassifier.__new__(BinaryStateClassifier)
        vars(classifier).update(value)
        diagnostics = {}
        classifier._fit(diagnostics)
        fields = ('center', 'scale', 'weights', 'hinge_weights', 'active', 'bias',
                  'ready', 'pending', 'fits')
        return {key: getattr(classifier, key) for key in fields}, diagnostics
    raise ValueError('unsupported pure-compute operation')


def main():
    # One optional CPU process, one BLAS thread; leave cores available to control.
    if hasattr(os, 'nice'):
        os.nice(10)
    limit = int(sys.argv[1]) * 1024**2
    if sys.platform == 'linux':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    source, destination = sys.stdin.buffer, sys.stdout.buffer
    while True:
        data = read_frame(source)
        if data is None:
            return
        # Bytes are supplied only by our parent through a private anonymous pipe.
        # No user file, network body or persisted pickle enters this protocol.
        request_id, operation, value, options = pickle.loads(data)
        try:
            result = compute(operation, value, options)
            reply = (request_id, True, result)
        except Exception as exc:
            reply = (request_id, False, (type(exc).__name__, str(exc)))
        write_frame(destination, reply)


if __name__ == '__main__':
    main()
