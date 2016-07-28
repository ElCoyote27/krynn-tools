# python Script to decode samsung SSD Firmware .enc files
# by HaQue 06-May-2015

# NO ERROR CHECKING IS DONE!
# Input file should be an encoded .enc file.
# Output file will be input filename appended with ".decoded".

# USEAGE: python dsssd.py xxxxx.enc
# Example: python samsung_ssd_decode.py test.enc
import sys
lookup = [0x0f,0x00,0x0e,0x01,0x0d,0x02,0x0c,0x03,0x0b,0x04,0x0a,0x05,0x09,0x06,0x08,0x07]
decFile = open(sys.argv[1] + '.decoded', 'wb')

b = bytearray(open(sys.argv[1], 'rb').read())
for i in range(len(b)):
    b[i] = (lookup[b[i] >> 0x04 & 0x0F] << 0x04) | (b[i] & 0x0F)
open(sys.argv[1] + '.decoded', 'wb').write(b)
