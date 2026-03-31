import tensorrt as trt

logger = trt.Logger(trt.Logger.INFO)
runtime = trt.Runtime(logger)

with open("model/car.engine", "rb") as f:
    engine_data = f.read()

engine = runtime.deserialize_cuda_engine(engine_data)

print("Engine type:", type(engine))
print("All attributes and methods:")
print(dir(engine))
