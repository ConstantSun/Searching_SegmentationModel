model = net
input = torch.randn(1, 3, 256, 256)
macs, params = profile(model, inputs=(input, ))

from thop import clever_format
macs, params = clever_format([macs, params], "%.3f")
print("macs: ", macs, "params: ", params)
