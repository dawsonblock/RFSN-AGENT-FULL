import unittest
from services.executor.capsule import Capsule


class TestCapsule(unittest.TestCase):
    def test_docker_args_tmpfs(self):
        c = Capsule(
            container_name="test-cap",
            image="test-img",
            repo_host_path="/host/repo",
            work_type="tmpfs",
            network_mode="none",
        )
        args = c.docker_args()
        self.assertIn("--read-only", args)
        self.assertIn("--tmpfs", args)
        self.assertIn("/work/repo:rw,exec,nosuid,nodev,size=512m", args)
        self.assertIn("-v", args)
        self.assertIn("/host/repo:/mnt/repo_ro:ro", args)

    def test_docker_args_bind(self):
        c = Capsule(
            container_name="test-cap",
            image="test-img",
            repo_host_path="/host/repo",
            work_type="bind",
            work_host_path="/host/work",
            network_mode="bridge",
        )
        args = c.docker_args()
        self.assertIn("/host/repo:/mnt/repo_ro:ro", args)
        self.assertIn("/host/work:/work/repo:rw", args)
        self.assertIn("--network", args)
        self.assertIn("bridge", args)

    def test_entrypoint_cmd(self):
        c = Capsule("test", "img", "/repo")
        cmd = c.entrypoint_cmd("my_cmd")
        self.assertEqual(cmd[0], "img")
        self.assertEqual(cmd[1], "bash")
        self.assertIn("cp -r /mnt/repo_ro/. /work/repo/", cmd[3])
        self.assertIn("exec my_cmd", cmd[3])


if __name__ == "__main__":
    unittest.main()
