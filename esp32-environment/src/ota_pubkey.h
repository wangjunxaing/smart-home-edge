/*
 * OTA 固件签名公钥（5.2）
 * =====================
 * RSA-2048 公钥（SubjectPublicKeyInfo DER，base64 存储，运行时解码）。
 * 与服务器 /home/ubuntu/server/keys/ota_rsa_priv.pem 配对（2026-09-08 生成，私钥仅存服务器）。
 * 泄露公钥无害；替换密钥时同步替换本文件并在服务器重新生成密钥对。
 */
#ifndef OTA_PUBKEY_H
#define OTA_PUBKEY_H

static const char OTA_PUBKEY_DER_B64[] =
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAo87nucrcWUqR9zcW9y/tKmj0KFiDjWD3bl5fSNw6bSye5Fw3Yu4YjB95lpKS36qmQBbE6rBoXLft7FId8W0Cmf2iyntY2fONLILczqzYVsFv4rz2vb4QD0SB9JKSwKaUHtZfjzAVHua+LXg2U9koGEJvhh+83T5f4DIaYCRaFQUiyZEI54nJc1FnZGeBP3UhkWZWAI31ZJJHStszC16pYhiOXXfMggUXnNYg0D3XqcwR9Q+7P3PpFky9SM7iiDf6EsjTa02kQx+VMqXmHuurP6rP9X2kUwnu11cuzuOzzuE4/lagVOZrXy98/HIPG10I1+9F8aO2QS9v4nBRoKm1/wIDAQAB";

#endif // OTA_PUBKEY_H
