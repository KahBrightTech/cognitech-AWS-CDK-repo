import json

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk.assertions import Template

app = cdk.App()
s = cdk.Stack(app, "s", env=cdk.Environment(account="111122223333", region="us-east-1"))
v = ec2.Vpc(s, "v")
g = ec2.SecurityGroup(s, "g", vpc=v)
g.add_ingress_rule(ec2.Peer.security_group_id("sg-0123456789abcdef0"), ec2.Port.tcp(443), "sg")
g.add_ingress_rule(ec2.Peer.prefix_list("pl-0123456789abcdef0"), ec2.Port.tcp(443), "pl")

t = Template.from_stack(s)
print(json.dumps(t.find_resources("AWS::EC2::SecurityGroup"), indent=1)[:2000])
print("INGRESS:", json.dumps(t.find_resources("AWS::EC2::SecurityGroupIngress"), indent=1)[:1200])
