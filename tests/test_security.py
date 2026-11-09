import unittest

from sharebox.security import (
    hash_password,
    password_problem,
    sha256_file,
    token_fingerprint,
    verify_password,
)


class PasswordTest(unittest.TestCase):
    def test_hash_and_verify(self):
        salt, digest, iterations = hash_password("Passw0rd!")
        self.assertTrue(verify_password("Passw0rd!", salt, digest, iterations))
        self.assertFalse(verify_password("passw0rd!", salt, digest, iterations))
        self.assertFalse(verify_password("", salt, digest, iterations))

    def test_salt_is_random(self):
        first = hash_password("same-password")
        second = hash_password("same-password")
        self.assertNotEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])

    def test_password_policy(self):
        self.assertIsNotNone(password_problem("1234567"))
        self.assertIsNotNone(password_problem("12345678"))
        self.assertIsNone(password_problem("abcd1234"))

    def test_token_fingerprint_not_reversible(self):
        token = "secret-token"
        self.assertEqual(token_fingerprint(token), token_fingerprint(token))
        self.assertNotEqual(token_fingerprint(token), token)
        self.assertEqual(len(token_fingerprint(token)), 64)

    def test_sha256_file(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.txt"
            path.write_bytes(b"hello")
            self.assertEqual(
                sha256_file(path),
                "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            )


if __name__ == "__main__":
    unittest.main()
